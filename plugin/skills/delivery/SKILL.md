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

## 1. Back up early, CI once

Work never lives only on this Mac, and a backup costs no CI run:

```bash
dev branch ls            # every branch: where it lives, commits only here, last backup, embargo, PR, task
dev backup               # copy this branch to the remote, only when that push starts no CI run
dev pr create --draft    # an early draft PR: no CI where the workflows skip drafts
dev pr ready             # once, when the local suite is green: starts the one CI run
```

- `dev backup` reads the CI configuration of the commit it sends and pushes only when it shows no run starts
  (GitHub Actions, GitLab, Bitbucket, Azure; anything it cannot read means it does not push). It never pushes the
  default branch, a long-lived branch, or a branch a workflow deploys from (that push is a release step, made with
  `dev push`), never forces, stops at a diverged remote branch, skips WIP commits, and scans exactly the commits the
  remote lacks for secrets. `--accept-ci` pushes anyway and counts against the push budget. `dev sync` backs up every
  hour and at the end of a session, for commits older than `backup.after_minutes` (30).
- **The backup policy** is per project: `dev backup policy on|off|ask`. A private repository is on by default and a
  public one ask, because a backup there publishes the branch. An `on` keeps the visibility it was decided for: when
  the repository no longer reads as private and the visibility differs (or none was recorded), backups wait for the
  developer again. Changing it is the developer's own decision: it needs their typed `grant backup <project>`, and
  so does `dev backup --public`.
- **Embargo** a branch that must not leave this Mac, such as an unshipped security fix:
  `dev branch embargo <branch> --why "…"`. Its commits stay held after a merge into another branch, and an unreadable
  handoff note stops every backup. Lifting it needs the developer's `grant embargo <branch>`.
- **Draft early, ready once.** With `delivery.draft_pr` on, `dev pr create` opens a draft unless `--ready`. A draft
  needs no local CI evidence where the workflows skip drafts (`dev ci audit --fix` writes that gate); `dev pr ready`
  needs the evidence for HEAD, because it starts the run. Until that run, `dev pr checks` says no checks yet, and
  `dev pr merge` waits.

## 2. Single change

1. Start the work item, then branch from an up-to-date default: `dev task start <id>`, `dev branch new feat/x` (it
   links the item to the branch, and the item's lock renews while you work there).
2. Work in small verified steps (targeted tests while iterating), committing with `dev commit --why … --verified …`;
   back up as you go (`dev backup`).
3. Before the first push that starts CI, run the full local equivalent of CI (lint, types, unit, integration, build,
   package).
4. `dev ci --run` (local CI evidence), then `dev push` once, `dev pr create` (or `dev pr ready` on your draft), and
   arm a watch that wakes you (see [After a push, merge or release](#after-a-push-merge-or-release)).

**Only a CI host setting changed since the local run?** Then the evidence carries over, and you need no new run:
every changed file is a committed GitHub workflow edit whose added or removed lines are blank, comments, or
`timeout-minutes` and `concurrency` (`group`, `cancel-in-progress`) settings, with no YAML anchors, aliases, merge keys,
tags or directives, and `actionlint` and `dev ci audit` pass on it. `dev push`, `dev pr create`, `dev pr ready`, `dev release`
and `dev ci --evidence` record a host-only marker and say what they checked. Any other change (a run command, a step,
a trigger, the permissions, the matrix, `runs-on`, another file) needs `dev ci --run`, and the refusal names the
first difference. `dev ci --run --resume` keeps the passed steps after a host-only change, except scanners that read
every file.

## 3. Branches or PRs that must land together

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
dev ci watch --wait --pr <n>                          # as a Monitor or background command; name its task id
```

- Ask other sessions working on those branches to commit locally and stop pushing; worktrees share the
  repository, so you can merge their local branches directly.
- Never merge the individual PRs one by one to "see what CI says." The integration PR supersedes them: when it is
  merged, GitHub marks them merged (their commits are included) or close them with a link.
- `dev pr merge` closes the items of the integration branch only. The branches it carried are merged too (by ancestry
  or by content): `dev task reconcile --apply` closes their items, and `dev branch prune` lists them for deletion.

## After a push, merge or release

After `dev push`, `dev pr create`, `dev pr ready`, `dev pr merge` or `dev release`, devkit follows the pipeline to
the end in the background (`dev ci watch`) and tells this session how it ended at its next hook: green, or red with
the first error, its class and the one next action (`cicd` skill, section 6). A hook runs only when the session
does something, so that watch never wakes an idle session. Never poll.

**Arm a wait that wakes you, in the same turn, and name its task id in your reply.** `dev watch --wait` is the one
wait for every devkit event: devkit's background watch feeds the CI result into it, so one wait covers CI and the
crew together (the `wait-and-wake` skill has the command for each agent). To follow one run job by job instead, arm
`dev ci watch --wait`: in Claude Code as a Monitor or a background command; in Codex, which has no background
wake-up, in the foreground with a timeout. Re-arm a wait that ends first (exit 3; `dev watches` and `dev ci watches`
print the command), and report the result when it fires. `dev ci status` is for a look at the runs; it wakes nobody.

```bash
dev watch --wait                                                 # Claude Code: Bash with run_in_background; every event, CI included
dev ci watch --wait --branch <branch> --sha <sha> --timeout 28   # Claude Code: the Monitor tool, timeout_ms 1800000
dev ci watch --wait --branch <branch> --sha <sha>                # Claude Code: Bash with run_in_background
dev ci watch --wait --pr <n>                                     # a PR's head, or its merge commit once merged
dev ci watch --wait --tag v<version>                             # a release tag
dev ci watches                                                   # live and expired watches, with the re-arm command
```

It prints a line per finished job and a last line `CI <word> · …`, and exits 0 passed, 1 failed, 3 the watch ended
first (re-arm it), 4 none. A Monitor expires after 30 minutes: when its last line says `CI still running`, re-arm it.
In Codex, if the run is still going when the foreground timeout ends, say plainly that you check it again at the
next turn. The Stop hook sends back a turn that sent work to origin and leaves its unfinished run with nothing that
wakes you, naming the exact command.

When the fix is not trivial and you lead the crew, hand it to the Release engineer (Link):
`dev crew brief link "Fix the failed CI run: <job>, <class>" --activity cicd`. In auto mode this happens at once.

## 4. When CI fails

1. `dev run view <run-id> --failed`—read the actual error.
2. Reproduce it locally with the same command (and same versions). Fix it. Re-run the full local suite.
3. Push once. One fix per push, never "try and see."
4. Not reproducible on macOS (Linux-only path, case-sensitive FS, missing CI secret, runner resources)? Say so,
   make the smallest targeted fix, push once, and explain the reasoning in the PR.
5. Flaky? Prove it (passes locally repeatedly, unrelated to the change, history of flakiness), record it in the
   handoff note, then `dev run rerun <id> --flaky "<reason>"`.

The devkit guard blocks a third push to the same branch within an hour and a blind rerun—that is the
signal to stop and reproduce locally.

## 5. Release

1. From the verified, green integration branch: merge with the repository's strategy (squash vs merge commit):
   `dev pr merge`. It marks the merged branch's open items done (the PR and the merge commit in their changelog) and
   deletes the branch on origin and here, with its clean worktree; run inside that worktree, it names
   `dev wt rm <branch>` instead. Arm a watch on the merge commit (`dev ci watch --wait --pr <n>`).
2. Reconcile: `dev task reconcile`, then `--apply`, or reopen an item with what is left. `dev release` refuses while
   items of branches merged since the previous tag are open; `--why "…"` overrides it on the record, only for an
   item that truly stays open.
3. Version: the project's own scheme, otherwise CalVer `YY.MM.PATCH` (`dev calver` prints the next one). Tag
   `vYY.MM.PATCH` (`git tag -s` if the project signs tags), push the tag, create the GitHub release with notes from the
   CHANGELOG: `dev release` does all three (the guard refuses a raw `gh release create`), and once the release is out
   (not a `--draft`) deletes the branches it merged. Arm a watch on the tag (`dev ci watch --wait --tag v<version>`).
   While `main`'s CI still runs on the merge commit, `dev release` does not wait when a commit with HEAD's tree (a
   parent of HEAD, or the PR head GitHub links to it) passed, in its pull request run, every job still running on HEAD:
   it names that commit and releases. A skipped, running or push-only run does not count, a job that runs only on push
   holds the release until it passes, and a failed run on HEAD still refuses.
4. Verify the published artefact: download/install it from the release and smoke-test it.
5. Update the handoff note: version, what shipped, follow-ups. `dev branch prune` shows no merged branch left.

**Merged branches go.** One merge test decides: the branch's tip is in `origin/<default>`, its PR from this repository
merged (merge, squash, or rebase), or every commit has an equivalent there (cherry-picked, or carried by a release
branch). `dev pr merge`, `dev release`, the nightly `gc`, `dev wt rm`, `dev free`, `dev wt ls`, and
`dev branch clean` all use it, so a squash- or cherry-pick-merged branch reads as merged, not unpushed.
`dev branch prune` lists the rest; `--apply` deletes on origin, an irreversible action that a crew member never runs
and the main session runs under the gate (`--local` stays on this Mac). On GitHub, `dev delivery apply` sets
`delete_branch_on_merge` and `dev doctor` reports it. A `dev sync --offline` or `--no-push` deletes nothing on origin.

## 6. GitOps rules

- Git is the source of truth for code, configuration, infrastructure, and deployment manifests.
- Environments change only through merged PRs and tags (pipelines deploy them); no manual changes, no drift. If an
  emergency manual change happened, capture it in git immediately.
- Secrets live in the secret manager / CI secrets, referenced by name; never in the repository.
