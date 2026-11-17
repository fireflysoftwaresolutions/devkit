---
name: devsecops
description: Security across the delivery lifecycle—threat modeling new features, secrets hygiene, dependency and supply chain checks (SCA, SBOM, signing), SAST, container and IaC scanning, least-privilege CI, OWASP ASVS / Top 10 checks, and `dev sec scan`. Use when designing a feature that touches auth, data, money, files, external input or infrastructure; before a release; when adding or updating dependencies, Dockerfiles, Terraform/Kubernetes or GitHub Actions; when reviewing security-sensitive code; and when a session runs an older devkit copy than the pinned install.
---

# DevSecOps

devkit is not a security boundary: agents run with the permissions of their own settings (Claude Code's
permission mode, Codex's sandbox and approval policy), and devkit refuses no tool call. Threat-model the agent's own
reach like any other actor with the developer's credentials, and recommend the controls that do hold: the agent's
permission mode, branch protection and required reviews on the Git host, and narrow tokens (`SECURITY.md`). Use
`configuration` when changing devkit's settings. A skipped or unavailable scanner establishes no coverage.

Security is part of done. Fix findings at the source in the same branch; never silence a scanner without a written
reason (a suppression with a comment and a ticket).

## 1. Design: threat model (new features, new integrations)

STRIDE per data flow, in five minutes: who are the actors, what crosses a trust boundary, what is stored where. For
each boundary, ask about Spoofing (authn), Tampering (integrity, signatures), Repudiation (audit log), Information
disclosure (authz per resource, PII), Denial of service (limits, timeouts), and Elevation of privilege (roles,
injection).
Record the threats and mitigations in the plan or the initiative's Decisions.

## 2. Code (OWASP ASVS level 2 as the default bar)

- Validate input at the boundary; parameterised queries; encode output for its context; safe deserialisation.
- AuthZ checked per operation and per resource (ownership), not only per role. Deny by default.
- Secrets only from the environment or the secret manager; never in code, tests, logs, fixtures, or handoff notes.
- Crypto: platform libraries, no home-made primitives; TLS everywhere; passwords with argon2/bcrypt.
- Logs: structured, no secrets, no personal data; security events (login, permission change, export) audited.

## 3. Dependencies and supply chain

- Lockfiles committed; new dependencies need a reason (maintenance, licence, size); prefer the platform.
- `dev sec scan` before a release and after dependency changes: secrets (gitleaks), vulnerable dependencies
  (osv-scanner), code patterns (Semgrep), filesystem and IaC misconfiguration (Trivy), and GitHub Actions (zizmor).
- Read each scanner's result. Operational failures, timeouts, empty or invalid reports, and Semgrep scan errors
  fail the command even when no findings were decoded. Partial findings remain visible; error summaries omit raw
  stderr and report contents that could expose secrets. Resolve the scanner error and rerun before treating that
  check as complete.
- Semgrep runs as a repository's qualification gate runs it, with the same verdict: serially (`--jobs 1`), without a
  per-rule wall-clock timeout, and incomplete on any error, rule timeout, fixpoint timeout, or skipped rule. The scan
  names each one by type, path and rule (8, then a count). Two registry rules that only match code importing their
  package (boto3 or botocore; flask) are left out of a tree that never imports it, and the scan says so.
- `dev sec scan` waives nothing: a finding the repository reviewed and accepted still shows here. Its disposition
  lives in the repository's own qualification gate (devkit's: `scripts/security_corpus.py`).
- Missing optional tools are reported and skipped; a successful exit does not prove coverage of those checks.
  OSV's legitimate no-packages result remains accepted without JSON output. Semgrep informational findings alone
  do not fail the scan. Record these limits when reporting security verification.
- Release artifacts: an SBOM (`syft`), signed images and artifacts (`cosign`), provenance where the pipeline supports it.
- Renovate or Dependabot with grouped, scheduled updates; security updates immediately.

## 4. CI/CD (GitHub Actions)

- `permissions:` least privilege at workflow and job level (`contents: read` by default).
- Pin third-party actions to a full commit SHA; avoid `pull_request_target` with untrusted code; never echo secrets.
- OIDC to cloud providers instead of long-lived keys; environments with required reviewers for production.
- Branch protection: required checks, reviews, signed commits where the team uses them; CODEOWNERS for sensitive
  paths.

## 5. Containers and infrastructure

- Minimal, pinned base images; multi-stage builds; non-root user; read-only filesystem where possible; healthcheck.
- No secrets in images or build args; `.dockerignore` covers `.env*`, keys, and VCS data.
- Terraform/Kubernetes: scan with `trivy config`; no public buckets or wildcard IAM; resource limits and network
  policies on Kubernetes.

## 6. devkit's own install

devkit's hooks and `dev` run a pinned, read-only copy of a release, recorded in `install.json`.

- **An older copy never writes managed config** (the agents' settings and hooks, `CLAUDE.md`, `AGENTS.md`, the global
  git config). A session that still runs one skips those writes, records `config.repair-skipped`, says once to
  restart, and hands its background work to the pinned release. Tell the developer; never repair the files by hand.
- **Restart sessions after an update.** `dev install` and `dev update` list the Claude Code and Codex sessions that
  still run devkit and ask once (`--yes` goes on). Each listed session keeps its old hooks until it restarts.
- **Never run devkit from an old release folder** (`~/.local/share/devkit/releases/<older>/bin/dev`) or point a
  hook or script at one: use `dev` on the PATH, and read a copy's files with a file tool. `dev install` retires the
  older copies (their `bin/dev` runs the installed release), and `dev install --version <version>` restores one for
  a rollback.
- A backup policy keeps the repository visibility it was decided for: when a private repository turns public, or an
  `on` was recorded with no visibility or another one, devkit asks again before a backup publishes a branch. A
  policy decided while the visibility could not be read records `unknown`.
- **Deleting branches on origin cannot be undone there.** `dev branch prune --apply` (without `--local` or
  `--offline`) and `dev sync --scheduled` can delete merged branches on origin, like `git push --delete`. devkit does
  not stop them: protect the branches that must stay on the Git host.

## 7. Before you say it is done

`dev sec scan` clean (or every finding fixed or justified), `/security-review` for auth, input handling, crypto or
secrets changes, and the threats and decisions recorded. `dev commit` also refuses a commit when gitleaks finds a
secret in the staged changes; a raw `git commit` is not checked.
