# Delivery

GitHub CI confirms that work is ready; it is not where you find out whether it works. Each push costs a full CI
cycle (often 30 minutes or more), and each merge to the default branch can trigger the CI of every other open PR
again. devkit's method, which the global instructions and the devkit `delivery` skill teach both agents, is
**integrate and verify locally, push once, get one CI run.**

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/diagrams/delivery-dark.svg">
  <img src="images/diagrams/delivery-light.svg" alt="Local-first delivery: feature branches are merged on one local integration branch and checked with the local equivalent of CI; only a green local run is pushed once as one PR, so CI runs once; a red CI run is reproduced and fixed locally; a green run is merged, tagged with a CalVer version, released, and verified." width="100%">
</picture>

## 1. Know the pipeline

```bash
dev ci                 # workflows, triggers, jobs, runners, matrices, setup actions, every run: command
dev ci --commands      # only the commands, in order
dev ci --runs          # plus the recent runs
```

Use the same tool versions as the setup steps (`setup-node`, `setup-java`, `setup-python`).

## 2. A single change

```bash
git fetch origin && git switch -c feat/x origin/main
# … small steps, targeted tests while iterating …
# full local equivalent of CI: lint, types, unit, integration (Testcontainers), build, package
dev sec scan            # secrets, dependencies, code patterns, IaC, workflows
git push -u origin feat/x
gh pr create --fill     # then fill the description (see Templates)
gh pr checks --watch
```

The pull request description follows the template in [Templates › Pull request
description](templates.md#pull-request-description): what and why, changes, how to verify (with the local results),
risk and rollback, links. The title is a Conventional Commit.

## 3. Branches or PRs that land together

```bash
git fetch origin
git switch -c release/26.11.4 origin/main
git merge --no-ff origin/feat/a          # oldest dependency first
git merge --no-ff feat/b                 # local branches of other worktrees work too: same repository
# resolve conflicts once; bump the version, CHANGELOG and docs once
# run the full local suite on the result
git push -u origin release/26.11.4
gh pr create --fill --title "Release 26.11.4" --body "Integrates #12, #17, #22"
gh pr checks --watch
```

Ask the sessions working on those branches to commit locally and stop pushing. Never merge the individual PRs one
after another to see what CI says; the integration PR supersedes them. `dev calver` prints the next version and the
release branch name when the project uses CalVer.

## 4. When CI fails

1. Read the real error: `gh run view <run-id> --log-failed`.
2. Reproduce it locally with the same command and versions, fix it, run the full local suite.
3. Push once. One fix per push, never a "try this and see."
4. If the failure truly cannot be reproduced on macOS (Linux-only paths, case-sensitive file system, a CI-only
   secret, runner limits), say so, make the smallest targeted fix, push once, and explain it in the PR.
5. A flaky failure is re-run only with evidence (passes locally repeatedly, unrelated to the change, known history),
   recorded in the handoff note, with an explicit acknowledgement:

   ```bash
   DEVKIT_FLAKY=1 gh run rerun <run-id> --failed
   ```

## 5. Release

1. Run `dev sec scan` on the integration branch and fix or justify every finding.
2. From the verified, green integration branch, merge with the repository's strategy (squash or merge commit). A
   squash message repeats the PR title and says what changed and why in one or two sentences, with no attribution
   lines.
3. Tag (signed if the project signs tags), push the tag, and create the release with notes from the CHANGELOG:
   `gh release create vX.Y.Z --notes-file <notes>`. `dev calver` gives the next CalVer version.
4. Download or install the published artifact and smoke-test it.
5. Update the handoff note (version, what shipped, follow-ups) and mark the tasks done with `dev task done <n>`.

## 6. GitOps rules

- Git is the source of truth for code, configuration, infrastructure, and deployment manifests.
- Environments change only through merged PRs and tags that pipelines deploy; no manual changes, no drift. An
  emergency manual change is captured in git immediately.
- Secrets live in the secret manager or CI secrets, referenced by name, never in the repository. The guard refuses a
  commit when gitleaks finds a secret in it.
- Workflows use least-privilege `permissions:` and actions pinned to a full commit SHA; `dev sec scan` checks them with
  zizmor.

## How the guard enforces it

| Rule | Behaviour |
| --- | --- |
| Push budget | The third push of the same branch within 60 minutes is denied with instructions to reproduce locally. Prefix the push with `DEVKIT_LOCAL_CI_PASSED=1` only when the full local suite has just passed in the session. |
| Blind re-runs | `gh run rerun` is denied unless prefixed with `DEVKIT_FLAKY=1`. |
| Force pushes | Plain `--force`, `-f`, and `+refspec` are denied; `--force-with-lease` is allowed on your own branch, never to `main` or `master`. |
| Branch names | A branch that breaks `<type>/<ticket>-<topic>` cannot be created, renamed to, or pushed. |
| Secrets | A commit that contains a secret found by gitleaks is denied. |

The budget counts push attempts per repository and branch on this machine
(`~/.local/state/devkit/ci-pushes.json`). The full rules are in [Guard rules](guard.md).

See also [Guard rules](guard.md), [Commands › dev ci](commands.md#dev-ci-query---commands---runs) and
[Templates](templates.md).
