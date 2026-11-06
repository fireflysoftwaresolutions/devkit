---
name: delivery
description: Local-first GitOps delivery—integrate related branches or PRs on one local integration/release branch, run exactly what CI runs locally, push once and get a single CI run, fix CI failures by reproducing them locally, then release and verify. Use whenever you are about to push, open or merge PRs, combine branches, cut a release, or when CI is failing.
---

# Delivery: one integration, one push, one CI run

GitHub CI confirms; it is not where you find out whether the code works. Each push costs a full CI cycle and each
merge to the default branch can re-trigger the CI of every other open PR.

## 0. Know the pipeline

```bash
dev ci                 # workflows, jobs, runners, matrices and every `run:` command
dev ci --commands      # just the commands, in order
dev run ls             # recent runs and their state
```

Match tool versions to the setup steps (`actions/setup-node` → Node major, `setup-java` → JDK, `setup-python`).

## 1. Single change

1. Branch from an up-to-date default: `dev branch new feat/x`.
2. Work in small verified steps (targeted tests while iterating).
3. Before the first push, run the full local equivalent of CI (lint, types, unit, integration, build, package).
4. `dev ci --run` (local CI evidence), then `dev push` once, `dev pr create`, and `dev pr checks --watch`.

## 2. Branches or PRs that must land together

```bash
git fetch origin
git switch -c release/<version> origin/main          # or integration/<topic>
git merge --no-ff origin/feat/a                      # repeat per branch, oldest dependency first
git merge --no-ff feat/b                              # local branches from other worktrees work too
# resolve conflicts once, here; then version bump, CHANGELOG, docs
<full local CI equivalent>                            # must be green before any push
dev ci --run                                          # the evidence dev push and dev pr create check
dev push
dev pr create --title "chore(release): <version>"     # the template, pre-filled; name the PRs it integrates
dev pr checks --watch
```

- Ask other sessions working on those branches to commit locally and stop pushing; worktrees share the
  repository, so you can merge their local branches directly.
- Never merge the individual PRs one by one to "see what CI says." The integration PR supersedes them: when it is
  merged, GitHub marks them merged (their commits are included) or close them with a link.

## CI is watched for you

After `dev push`, `dev pr create`, `dev pr merge` or `dev release`, devkit follows the pipeline to the end in the
background (`dev ci watch`) and tells this session how it ended: green, or red with the first error, its class and
the one next action (`cicd` skill, section 6). Run `dev ci watch` yourself in the background when you want to be
notified the moment it ends. Never poll.

When the fix is not trivial and you lead the crew, hand it to Link, the CI/CD and release engineer:
`dev crew brief link "Fix the failed CI run: <job>, <class>" --activity cicd`. In auto mode this happens at once.

## 3. When CI fails

1. `dev run view <run-id> --failed`—read the actual error.
2. Reproduce it locally with the same command (and same versions). Fix it. Re-run the full local suite.
3. Push once. One fix per push, never "try and see."
4. Not reproducible on macOS (Linux-only path, case-sensitive FS, missing CI secret, runner resources)? Say so,
   make the smallest targeted fix, push once, and explain the reasoning in the PR.
5. Flaky? Prove it (passes locally repeatedly, unrelated to the change, history of flakiness), record it in the
   handoff note, then `dev run rerun <id> --flaky "<reason>"`.

The devkit guard blocks a third push to the same branch within an hour and a blind rerun—that is the
signal to stop and reproduce locally.

## 4. Release

1. From the verified, green integration branch: merge with the repository's strategy (squash vs merge commit).
2. Version: the project's own scheme, otherwise CalVer `YY.MM.PATCH` (`dev calver` prints the next one). Tag
   `vYY.MM.PATCH` (`git tag -s` if the project signs tags), push the tag, create the GitHub release with notes from the
   CHANGELOG: `dev release` does all three (the guard refuses a raw `gh release create`).
3. Verify the published artefact: download/install it from the release and smoke-test it.
4. Update the handoff note: version, what shipped, follow-ups.

## 5. GitOps rules

- Git is the source of truth for code, configuration, infrastructure, and deployment manifests.
- Environments change only through merged PRs and tags (pipelines deploy them); no manual changes, no drift. If an
  emergency manual change happened, capture it in git immediately.
- Secrets live in the secret manager / CI secrets, referenced by name; never in the repository.
