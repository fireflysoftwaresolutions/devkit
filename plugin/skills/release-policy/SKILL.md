---
name: release-policy
description: Understand, recommend and put in place a project's or initiative's delivery policy—versioning (CalVer or SemVer), version bumps, test gate, changelog, release flow, publishing and deployment—using `dev delivery` (detect, recommend, gaps) and `dev delivery apply` (record it in the handoff repository, open tasks for the gaps). Use before a project's first release, when a release, tag, version bump or publish is requested, when a repository has no clear release procedure, and when projects must release together (release trains).
---

# Release policy

Every project has one recorded way to version, test, release, and ship. It lives in the handoff repository
(`policies/<project id>.json`, or `policies/initiatives/<slug>.json` for a release train), sessions see it in their
context, the guard refuses tags that break it, and `dev release` follows it.

## 1. Find out where the project stands

```bash
dev delivery            # detected (from the repository) · recorded or recommended policy · gaps with fixes
dev delivery --json     # the same, for reasoning
```

It reads version tags, version files (`package.json`, `pom.xml`, `pyproject.toml`, `VERSION`, …), release tools
(release-please, semantic-release, goreleaser, git-cliff), CHANGELOG, CI provider, the commands CI runs (the test
gate), the branch model, and what the project publishes or deploys.

## 2. Decide (keep what works; change only with a reason)

| Situation | Versioning | Why |
| --- | --- | --- |
| The repository already has a consistent scheme | keep it | consumers and automation depend on it |
| A library, SDK, framework, or package others depend on | **SemVer** `vMAJOR.MINOR.PATCH` | the number tells consumers whether an upgrade can break them |
| An app, service, platform, internal tool, or docs site | **CalVer** `vYY.MM.PATCH` (devkit default) | the date tells everyone how current a deployment is; nobody depends on its API through a version range |
| Projects released together (platform, BOM, parent POM) | a **release train** on the initiative | one version and one order for the set; each member keeps its own scheme |

Bumps:

- **CalVer:** `dev calver --next`. `PATCH` restarts at 0 each month: 26.11.0, 26.11.1, then 26.12.0.
- **SemVer** follows Conventional Commits since the last tag. `BREAKING CHANGE` or `!` bumps major; `feat` bumps
  minor; `fix` and `perf` bump patch. Before 1.0.0, breaking changes bump minor.

The test gate is exactly what CI runs (`dev ci --commands`), and it runs locally before the one push. For the
changelog, use Keep a Changelog sections generated with git-cliff from Conventional Commits, edited for readers.

## 3. Record it, and turn the gaps into work

```bash
dev delivery apply --why "first release policy: service, CalVer, Maven verify gate" --tasks
dev delivery apply --set versioning=semver --set test_gate="./mvnw -B verify" --why "it is a library now"
dev delivery apply -i <initiative> --set release_train="bom → parent → services, monthly" --why "…"
```

Every change bumps the policy's version and records who changed it, when and why. `--tasks` opens a work item for
each gap, such as no CI, no test gate, no changelog, or no release workflow.

Branch cleanup is part of the policy: a merged branch is deleted. `dev pr merge` and `dev release` prune the
branches they merge or find merged, `dev branch prune` lists the rest (`--apply` deletes them with their clean
worktrees), and GitHub's `delete_branch_on_merge` deletes a PR's branch when GitHub merges it. `dev doctor` reports
that setting, and `dev delivery apply` turns it on.

## 4. Release by the policy (delivery skill, global rules §7)

1. **Integrate.** Bring everything that ships into `release/<version>` from an up-to-date `origin/<default>`.
2. **Run the test gate locally,** then push once and open one PR.
3. **Bump the version files the policy names,** and write the CHANGELOG section in the same branch.
4. **Reconcile the work items:** `dev task reconcile`, then close (`--apply`) or reopen what it lists. `dev release` and
   `--dry-run` refuse while items of branches merged since the previous tag are open. `--why "…"` releases anyway and
   is recorded in the audit and the output: use it only for an item that truly stays open.
5. **Merge**, then tag on the default branch: `v<version>`, in the policy's format. The guard refuses others.
6. **Publish and verify the artifact:** install or download it, and check the release notes.
7. **Hotfix:** branch `hotfix/<version>` from the release tag. Fix it with a test, release the next patch, then
   merge the fix back to the default branch.
8. **Rollback:** re-deploy the previous tag. Never move or delete a published tag; publish a new version instead.

## 5. Release trains (initiatives)

- Record the order and the compatibility rule in the initiative's policy, for example: "consumers adopt exactly one
  published framework version through the parent POM and BOM; published versions are never overwritten."
- Release the members in dependency order, and record each released version in the initiative note's Decisions.
- A member project's own policy still governs its tags.

## Done when

- `dev delivery` shows a recorded policy and no high gaps.
- The release followed it: test gate green locally, one PR, one CI run, tag in the right format, CHANGELOG updated,
  artifact verified.
- No open item of a merged branch, and no merged branch left (`dev task reconcile`, `dev branch prune`).
