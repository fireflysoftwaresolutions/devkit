---
name: devsecops
description: Security across the delivery lifecycle — threat modelling new features, secrets hygiene, dependency and supply-chain checks (SCA, SBOM, signing), SAST, container and IaC scanning, least-privilege CI, OWASP ASVS / Top 10 checks, and `dev sec scan`. Use when designing a feature that touches auth, data, money, files, external input or infrastructure; before a release; when adding or updating dependencies, Dockerfiles, Terraform/Kubernetes or GitHub Actions; and when reviewing security-sensitive code.
---

# DevSecOps

Security is part of done. Fix findings at the source in the same branch; never silence a scanner without a written
reason (a suppression with a comment and a ticket).

## 1. Design: threat model (new features, new integrations)

STRIDE per data flow, in five minutes: who are the actors, what crosses a trust boundary, what is stored where. For
each boundary ask: Spoofing (authn), Tampering (integrity, signatures), Repudiation (audit log), Information
disclosure (authz per resource, PII), Denial of service (limits, timeouts), Elevation of privilege (roles, injection).
Record the threats and mitigations in the plan or the initiative's Decisions.

## 2. Code (OWASP ASVS level 2 as the default bar)

- Validate input at the boundary; parameterised queries; encode output for its context; safe deserialisation.
- AuthZ checked per operation and per resource (ownership), not only per role. Deny by default.
- Secrets only from the environment or the secret manager; never in code, tests, logs, fixtures or handoff notes.
- Crypto: platform libraries, no home-made primitives; TLS everywhere; passwords with argon2/bcrypt.
- Logs: structured, no secrets, no personal data; security events (login, permission change, export) audited.

## 3. Dependencies and supply chain

- Lockfiles committed; new dependencies need a reason (maintenance, licence, size); prefer the platform.
- `dev sec scan` before a release and after dependency changes: secrets (gitleaks), vulnerable dependencies
  (osv-scanner), code patterns (Semgrep), filesystem and IaC misconfiguration (Trivy).
- Release artefacts: SBOM (`syft`), signed images and artefacts (`cosign`), provenance where the pipeline supports it.
- Renovate or Dependabot with grouped, scheduled updates; security updates immediately.

## 4. CI/CD (GitHub Actions)

- `permissions:` least privilege at workflow and job level (`contents: read` by default).
- Pin third-party actions to a full commit SHA; avoid `pull_request_target` with untrusted code; never echo secrets.
- OIDC to cloud providers instead of long-lived keys; environments with required reviewers for production.
- Branch protection: required checks, reviews, signed commits where the team uses them; CODEOWNERS for sensitive
  paths.

## 5. Containers and infrastructure

- Minimal, pinned base images; multi-stage builds; non-root user; read-only filesystem where possible; healthcheck.
- No secrets in images or build args; `.dockerignore` covers `.env*`, keys and VCS data.
- Terraform/Kubernetes: scan with `trivy config`; no public buckets or wildcard IAM; resource limits and network
  policies on Kubernetes.

## 6. Before you say it is done

`dev sec scan` clean (or every finding fixed or justified), `/security-review` for auth, input handling, crypto or
secrets changes, and the threats and decisions recorded. The devkit guard also refuses a commit when gitleaks finds a
secret in the staged changes.
