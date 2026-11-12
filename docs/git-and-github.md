# Git and GitHub

`dev` wraps git and the GitHub CLI for everyday delivery: branches, commits, rebases, pushes, pull requests, CI
runs, releases, and issues. Each command checks the workspace rules before it acts, so a mistake is refused with the
one command that fixes it instead of costing a CI run. People and agents use the same commands.

Every command prints one line or a short table, plain text when its output is not a terminal; the read commands also
take `--json`. A refusal exits with `2` and prints the reason and the fix; a failed operation exits with `1`. The
reference of every option is in [Commands › Git and GitHub](commands.md#git-and-github).

## The everyday flow

```bash
dev branch new feat/invoice-export --ticket PROJ-142    # from an up-to-date origin/main
# … work in small, tested steps …
dev commit -a -m "feat(invoice): add CSV export" --why "Finance copies invoices by hand every month." \
  --verified "python3 -m unittest tests.test_export (8 tests pass)"
dev ci --run                                            # run what CI runs, here; record the evidence
dev push                                                # one push, after the checks
dev pr create --dry-run | sed '1,/^---$/d' > body.md   # the PR body, pre-filled from the template
$EDITOR body.md                                         # write "What and why"
dev pr create --body-file body.md
dev pr checks --watch                                   # wait for CI; failing jobs come with their logs
dev pr merge                                            # when the checks pass and the reviews allow it
```

What it looks like:

```text
$ dev branch new "feat/Invoice Export" --ticket PROJ-142
✓ feat/PROJ-142-invoice-export from origin/main @ 2493cca
$ dev commit -m "feat(invoice): added export"
✗ Commit message `feat(invoice): added export` starts with `added`: use the imperative mood (`add`, not `added`, `adds` or `adding`).
$ dev commit -m "feat(invoice): add CSV export" --body "Finance needs monthly invoices in their spreadsheet."
✓ d75a9f9 feat(invoice): add CSV export · 1 file(s) +1 −0 · feat/PROJ-142-invoice-export ↑1
$ dev ci --run
  ✓ test › Lint  0s
  ✓ test › Unit tests  0s
  – test › Install  dependency install (your local environment is used)
  – test › Upload coverage  side effect (publish, deploy, push, system install)
✓ local CI passed: 2 step(s) in 0s · 2 skipped · evidence recorded for tree fb61250 · dev push accepts it
$ dev push
✓ pushed feat/PROJ-142-invoice-export → origin (1 commit(s) · first push) @ d75a9f9 · open the PR: dev pr create
```

## Branches

`dev branch new <type>/<topic>` fetches `origin`, then creates the branch from `origin/<default>` and switches to it.
Your uncommitted changes come along. The name is normalized to the [naming rule](guard.md#branch-names):
`feat/Invoice Export` with `--ticket PROJ-142` becomes `feat/PROJ-142-invoice-export`. A name that breaks the rule, or
that already exists here or on `origin`, is refused.

`dev branch rename <type>/<topic>` gives a harness-made branch (`claude/vigilant-jang-ec71c0`, a detached HEAD) a
proper name before its first push. It refuses to rename the default branch and permanent branches, and a branch that
is already on `origin`, because the remote branch and its PR would stay behind.

`dev branch ls` prints the branch map: one row per local branch with where it lives (local only, or local and
`origin`), how far it is ahead (↑) and behind (↓) its upstream and the default branch, the commits that exist only on
this Mac and the branch that carries them, its last backup, an embargo, its worktree, its pull request, and its
linked task. Each row ends with whether its work is safe off this Mac, or why not:

```text
  fireflysoftwaresolutions/devkit · 4 branches · 1 not backed up ──────────────────────────────────
  * release/26.11.3        local + origin  ↑19 ↓0 · 262 ahead of main · not backed up: the backup
                                           policy of fireflysoftwaresolutions/devkit is ask, the
                                           default for a public repository, and the developer has
                                           not answered
    feat/curl-install      local only      merged into release/26.11.3 · nothing to back up
    main                   local + origin  ↑0 ↓0 · default branch · nothing to back up
  Backup policy: the default: on for a private repository, ask for a public one (dev backup policy).
  Backups wait for commits older than 30 min (backup.after_minutes).
```

`--offline` skips the fetch and the GitHub lookups, and `--json` gives the same rows to tools. `dev status` and the
handoff note's generated block show the same rows, the dashboard shows them in each project's Branches tab, and the
session-start context names the branches that exist only on this Mac and the embargoed ones.
`dev backup` copies a branch that is not backed up to the remote when that push starts no CI run, and
`dev branch embargo` keeps one off the remote (agents' pushes from another project's copy are held from 26.11.4); see
[Delivery › Back up early, CI once](delivery.md#back-up-early-ci-once).

`dev branch clean` lists the local branches that can go: merged by the merge test of `dev branch prune` (below), so
also by squash or cherry-pick, or gone upstream with every commit on the remote. It keeps the current branch,
permanent branches, branches checked out in a worktree, and branches with commits that exist only here and are not
merged, and says why. Nothing is deleted until you add `--apply`. `dev branch prune` also covers remote branches and
worktrees.

### Close merged branches

A merged branch has nothing left to give, so devkit closes it, on `origin` and on this Mac. One merge test decides
when a branch counts as merged into the default branch (`origin/<default>`), and every output says which way it holds:

| How | When | The output says |
| --- | --- | --- |
| Ancestor | The branch's tip is in the default branch: a merge commit or a fast-forward | `merged: its tip is in origin/main` |
| Pull request | A merged PR of the branch (merge, squash, or rebase) had the branch's tip as its head, came from this repository (not a fork), and its merge commit is in the default branch | `merged: PR #7 was merged` |
| Content | Every commit of the branch has an equivalent in the default branch (`git cherry` shows only `-`): it was cherry-picked, or a release branch that merged carried it | `merged by content: 8 commits have an equivalent in origin/main` |

Anything else is not merged, with the count: `not merged: 1 of 3 commits not in origin/main`.

`dev branch prune` lists every local and remote branch with its state: merged and how, an open PR, not merged (with
its commits ahead and the age of its last commit), or protected. It fetches `origin` first and changes nothing else:

```text
fireflysoftwaresolutions/devkit · against origin/main · 18 local and 4 remote branches
  delete local  fix/guard-last-fixes  merged by content: 11 commits have an equivalent in origin/main
  delete remote fix/doctor-stacks-and-banner  merged by content: 11 commits have an equivalent in origin/main
  delete local  release/26.11.5  merged: its tip is in origin/main
  keep   local  hotfix/26.11.6  protected: the current branch
  keep   local  main  protected: the default branch
• dry run: nothing deleted · dev branch prune --apply deletes 18 branches and 14 worktrees
```

`dev branch prune --apply` deletes the merged branches: the remote branch (`git push origin --delete`, with a lease
on the commit it listed, so a branch someone pushed to since stays), then the local branch and its worktree when the
worktree is clean (the same check as `dev wt rm`). `--remote` or `--local` limits it to one side, `--offline` skips
the fetch and GitHub and leaves remote branches alone, and `--json` prints the rows. It never deletes:

- the default branch, `develop`, and the other permanent branches;
- the current branch, or the branch checked out in the main checkout;
- a branch with an open pull request, or a remote branch when devkit cannot read every open pull request (GitHub
  unreachable, 1,000 or more open, or another host);
- an embargoed branch, or anything while the embargo list cannot be read;
- a branch whose worktree has uncommitted changes, is locked, belongs to the Codex app, or has a live session;
- a branch whose worktree holds ignored files that no tool rebuilds, such as release evidence under `build/` (see
  [Disk and cleanup › What it reclaims](disk-and-cleanup.md#what-it-reclaims));
- a branch created less than 24 hours ago that has no commit of its own yet;
- a branch with a commit the default branch does not have by content.

Each deletion is audited as `branch.pruned`, with how the branch was merged and its last commit
(`dev audit --action branch.pruned`). Deleting on `origin` is an irreversible action: a crew member never runs
`dev branch prune --apply` without `--local` or `--offline`, and the main session runs it under the
[irreversible-action gate](guard.md#git). The listing, `--local`, and `--offline` need no grant.

The same merge test closes branches at four other moments:

- `dev pr merge` deletes the remote branch, then the local branch and its clean worktree. When it keeps the
  worktree, it prints the reason as a warning line of its own after the success line.
- `dev release`, once the release is out (not a `--draft`), deletes the branches the release merged (merged into it,
  and not into the previous tag), remote and local, and prints them. Branches merged before stay for the nightly
  job.
- The nightly `gc` job deletes a merged local branch that has not moved for `gc.grace_hours` (24), with its idle
  worktree. It deletes a merged remote branch only when its own pull request merged (from this repository, with
  the branch's tip as its head; a fork's pull request of the same name never counts) or devkit pushed it as a backup
  (`dev backup`, `dev sync`); anyone else's merged branch waits for `dev branch prune --apply`.
- The hourly backup no longer pushes a branch merged by content, or squash-merged by its own pull request, back to
  `origin`: the branch map counts it as merged, and `dev backup` skips it with the pull request's number
  (`merged: PR #7 was merged`).

`dev wt rm`, `dev free`, the unpushed count of `dev wt ls`, and `dev branch clean` use the same test, so a branch
merged by squash or cherry-pick reads as merged, not as unpushed.
[Disk and cleanup › Merged branches](disk-and-cleanup.md#merged-branches) has what the nightly job deletes and keeps.

On GitHub, turn on the repository setting that deletes a pull request's branch when it merges:
`dev delivery apply --why "…"` sets `delete_branch_on_merge` and records it in the delivery policy, and `dev doctor`
lists each GitHub project you administer where it is off. GitLab, Bitbucket, and Azure DevOps have their own setting;
`dev delivery apply` names it.

To get a deleted branch back, take its last commit from the audit record or the output and recreate it:
`git branch <name> <sha>` here, or `git push origin <sha>:refs/heads/<name>` on the remote.

## Commits

`dev commit -m "type(scope): summary"` commits the staged changes, every tracked change with `-a`, or only the paths
you list. More `-m` options add paragraphs, and `--body` adds the reason.

The message follows devkit's commit template, [`config/git/commit-template.txt`](../config/git/commit-template.txt):
the header, then why, the root cause of a fix, what changed, and how it was verified, then the trailers.
`dev commit` writes it for you:

```bash
dev commit parser.py -m "fix(parser): keep quoted commas" \
  --why "Rows with quoted commas were split in two." \
  --cause "the tokenizer split on every comma before it read the quotes" \
  --body "The tokenizer reads a quoted field whole." \
  --verified "python3 -m unittest tests.test_parser (12 tests pass)"
```

```text
fix(parser): keep quoted commas

Rows with quoted commas were split in two.

Root cause: the tokenizer split on every comma before it read the quotes

The tokenizer reads a quoted field whole.

Verified: python3 -m unittest tests.test_parser (12 tests pass)

Refs: PROJ-142
```

| Option | Writes |
| --- | --- |
| `--why` | The first paragraph: why the change is needed |
| `--cause` | The `Root cause: …` paragraph, right after the why: what caused the problem, as your reproduction proved it. Required for `fix` and `revert` commits; a placeholder such as `unknown`, `n/a`, or `tbd`, or a cause under 15 characters, is refused. The `root-cause` skill has the procedure that proves it |
| `--body`, more `-m` | What changed; repeat `--body` for more paragraphs. Without `--why`, `--body` is the reason, as before |
| `--verified` | The last paragraph, `Verified: …` (the label is added when you leave it out) |
| `--refs` | The `Refs:` trailer: `#12`, `owner/repo#12`, a URL, or a ticket key such as `PROJ-142`. Repeat it for more |
| `--breaking` | The `BREAKING CHANGE:` trailer: what breaks, and how to move to the new behavior |

- **`Refs:` from your task.** Without `--refs`, `dev commit` fills `Refs:` with the ticket of the task you started on
  this branch (`dev task start` links the branch to the task). A task's ticket is one of its links: `ticket:PROJ-142`,
  `issue:#12`, or a URL. When the started tasks on the branch name different tickets, nothing is filled, and the output
  names them so you can pick one with `--refs`. A message that already has a `Refs:` line keeps it.
- **No devkit task ids.** A message that names a devkit task id (`T-…`, `F-…`, `B-…`, or `S-…`) is refused, whether
  it is in the header, the body, or `--refs`. Task ids stay in the private handoff, whose session log already links
  each commit to its task.

| The header must | Refused example |
| --- | --- |
| Be a Conventional Commit, `type(scope): summary` | `Added invoice export` |
| Use a known type: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`, `deps` (or your `commits.types`) | `feature: export` |
| Use the imperative mood | `feat(invoice): added export` |
| End without a period | `feat(invoice): add CSV export.` |
| Fit in 72 characters | — |
| Not be a WIP marker | `wip: totals` (use `--wip`) |

- **Not on the default branch.** In a repository with an `origin`, a commit on `main` is refused with
  `dev branch new`, which takes your changes along. A branch with a non-conforming name is refused with
  `dev branch rename`.
- **No AI attribution.** `Co-Authored-By` and `Generated with` lines for Claude, Codex, ChatGPT, Copilot, Gemini,
  Cursor, and others are removed from the message, and the output says how many.
- **No secrets.** The guard's gitleaks scan runs on what the commit records.
- **Amend with care.** `--amend` keeps the message unless you give `-m`. Amending a commit that is already pushed works,
  and the output tells you that the next push needs `dev push --force-with-lease`.

### The template in your editor

`git commit` without `-m` opens the template when it is the repository's `commit.template`. `dev setup` asks for it
with your git hosts ("Open new commit messages with devkit's template?"), and the git step of `dev install`, which
`dev setup` runs, applies the answer. The answer is kept:

- **Yes:** it sets `commit.template` in each repository's own git config (never the global one), and in repositories
  you clone later on the next install, without asking again.
- **No:** nothing changes, and nobody asks again.
- **Not answered yet:** `dev install` asks in a terminal. Without one (a scheduled job), nothing changes, and the step
  says how to choose.

`dev setup commit-template` asks again, even after a no, and `dev setup commit-template --remove` takes devkit's
template out of every repository and records a no.

A repository with a template of its own, in its config or your global config, keeps it. A repository where you removed
devkit's (`git config --unset commit.template`) is never set again by `dev install`; only `dev setup commit-template`
sets it again. The repositories point at a copy, `~/.local/state/devkit/git/commit-template.txt`, that each install
refreshes. Because every line of the template is a comment, closing the editor without writing a message commits
nothing.

### Work in progress instead of a stash

The git stash is shared by every worktree of a repository, so another session can pop your changes. Save them as a
commit on your branch instead:

```bash
dev commit --wip -m "half-done totals"   # everything, untracked files too, as `wip: half-done totals`
dev commit --unwip                       # undo it; the changes come back as uncommitted
```

A WIP commit is never pushed: `dev push` refuses `wip`, `fixup!`, `squash!`, and `amend!` commits, and `--unwip`
refuses a WIP commit that is already on the remote. The guard refuses a bare `git stash`, and `dev git stash` points
to `--wip`.

## Rebase on the default branch

`dev rebase` fetches `origin` and rebases the current branch on `origin/<default>` (or `--base REF`). It needs a clean
tree; save unfinished work with `dev commit --wip` first. When a conflict stops it, it lists the files and the
commit it was applying; resolve them, `git add` them, and run `dev rebase --continue` (or `--abort`, or `--skip`).

On the default branch itself it only fast-forwards, and it refuses when local commits would be lost, with the command
that moves them to a branch. After a rebase, it reminds you to run the local suite again and, when the branch was
already pushed, to push with `--force-with-lease`.

## Local CI evidence

GitHub CI confirms; it does not find problems for you. `dev push`, `dev pr create`, and `dev release` therefore want
proof that what CI runs passed here, on exactly the files you push.

```bash
dev ci --run --dry-run    # the plan: which steps run here and which are skipped, with the reason
dev ci --run              # run them in order and record the evidence when all pass
dev ci --evidence         # does HEAD have evidence?
dev ci --mark-passed --note "./mvnw -B verify: 214 tests passed"   # you ran the suite yourself
```

`dev ci --run` reads the GitHub Actions workflows that run on pushes and pull requests and runs their `run:` steps in
order, with `bash -eo pipefail`, `CI=true`, and the step's environment, stopping at the first failure (`--keep-going`
runs on). Each step has 30 minutes (`--step-timeout`), and its log is kept in
`~/.local/state/devkit/ci/logs/<repo>/`. When macOS has only `python3`, a `python` shim stands in for what
`setup-python` provides on the runner.

Steps that cannot or must not run on your Mac are skipped, with the reason:

| Skipped | Why |
| --- | --- |
| Setup actions (`uses:`) | Your local toolchain stands in for them |
| Dependency installs (`npm ci`, `pip install`, `uv sync`, …) | Your local environment is used; nothing is reinstalled |
| Side effects: publishing, deploying, pushing, system installs, coverage uploads | They change things outside your Mac |
| A hosted runner's own setup: a step that deletes a path not inside the checkout (`rm -rf /opt/hostedtoolcache/…`), `docker` or `podman … prune`, commands run with `sudo`, OS package installs and updates (`apt-get`, `brew`, `dnf`, `apk`, `choco`, …), `playwright install` and `install-deps`, and `git config --global` or `--system` | They would delete your caches and images, install system packages, or rewrite your settings. devkit also reads the script a step hands to a shell with `-c` (`xvfb-run bash -c …`) |
| Steps that need CI secrets or write CI plumbing (`$GITHUB_ENV`, `$GITHUB_OUTPUT`) | They only make sense on the runner |
| Jobs that deploy or publish, run on Windows, or only on other events | They are not part of the check of a change |

A step that could be a real check but cannot run here (an unknown condition, an unresolved `${{ }}` expression, a
shell other than bash, a reusable workflow, a missing working directory) means no evidence is recorded. Run it
yourself, then record the result with `dev ci --mark-passed --note "<command>: <result>"`.

A step that names a Docker context (`DOCKER_CONTEXT=`, `docker --context`, `docker context use`, or a program's
`--docker-context`) runs only when this Mac's Docker CLI has a context of that name. Otherwise it is one of these
checks that cannot run here, and no evidence is recorded: the `default` context on a runner is the runner's own
engine, and running the step against your current context would test another engine.

`dev ci --commands` and the test gate that `dev delivery` detects leave out the same runner-only setup, so the three
agree on what runs here. Dependency installs such as `npm ci` stay listed.

**The evidence follows the files, not the commit.** It is recorded for the tree that was tested, including
uncommitted files, in `~/.local/state/devkit/ci/<repo>@<commit>.json` (kept 30 days). Commit exactly what you tested
and the evidence holds; change anything else and it no longer matches, and the refusal lists the files that changed
since the last run. When files change while the suite runs, nothing is recorded.

If the full suite passed in this session in another way, `dev push --ci-passed` (and the same option on
`dev pr create` and `dev release`) records an attestation for HEAD instead. Evidence from `dev ci --run` also lifts the
guard's [push budget](delivery.md#how-the-guard-enforces-it) for that push.

## Push once

`dev push` pushes the current branch to `origin` and sets its upstream. Before it pushes, it checks, in order:

1. The branch is not the default branch or another permanent branch, and its name follows the rule.
2. `git fetch` succeeded; devkit does not push on stale remote state.
3. `origin` has nothing you lack. If you rebased or amended, the push needs `--force-with-lease`, which is refused
   when it would drop someone else's commits.
4. No WIP, `fixup!`, `squash!`, or `amend!` commit would be pushed.
5. A first push is not behind `origin/<default>` (rebase first, or `--allow-behind`).
6. HEAD has local CI evidence (or `--ci-passed`).
7. The guard allows it: the push budget, the branch name, and the force rules.

`dev push --dry-run` shows what would be pushed, the evidence, and the pushes this hour. After the push, it prints the
PR's address and `dev pr checks --watch`, or `dev pr create` when there is no PR yet.

## Pull requests

### Create

`dev pr create` opens the PR of the pushed branch. HEAD must be what `origin` has, and a PR that is ready for review
needs local CI evidence.

A draft (`--draft`) opens without the evidence when no workflow runs on draft PRs, for example when the jobs have
`if: github.event.pull_request.draft == false`, which `dev ci audit --fix` writes. Opening it costs no CI run, so
`dev pr ready` asks for the evidence instead, before it starts the one run. Where a workflow runs on drafts, or devkit
cannot tell, a draft needs the evidence too, and the refusal names the workflow.

- **Title.** `--title`, or the first commit's header when it is a Conventional Commit, or one derived from the
  branch (`release/26.11.4` becomes `chore(release): 26.11.4`). It follows the repository's own title convention,
  because a squash merge makes it the commit on `main`. devkit reads the titles of the last 20 merged pull requests,
  bots left out, or the default branch's last commit subjects when GitHub lists fewer than five (merges, reverts, and
  WIP left out). When at least 80% of five or more are plain sentences, the convention is a plain imperative
  sentence: a capital first letter, no Conventional Commit prefix, no period, no WIP marker, and no length rule.
  Anything else keeps Conventional Commits. In a sentence repository, the default title is the first commit, or its
  Conventional Commit summary written as a sentence, and a Conventional Commit title is refused with the sentence to
  write. `dev pr ready`, `dev pr merge --squash`, and `dev gh pr create` and `merge` check the same convention.
- **Template.** The repository's own pull request template wins (`.github/pull_request_template.md` and the other
  places GitHub reads). Otherwise devkit uses its own in your `language`: `templates/github/pull_request.md` in
  English, `pull_request.es.md` in Spanish. `--lang es-ES` picks another one for this PR.
- **Pre-filled.** Changes lists the commits (without WIP commits). How to verify gets the local CI line, for example
  `Local CI: dev ci --run → passed (2 steps: Lint, Unit tests) @ d75a9f9`. Links gets the task ids, ticket keys, and
  `Closes #n` found in the branch name and the commits.
- **Written by you.** "What and why" and "How to verify" must contain text you wrote: comments, empty bullets, and
  `<placeholders>` do not count. A PR with either section empty is refused; `--draft` opens it anyway. Spanish
  headings count too: "Qué y por qué" and "Cómo verificarlo."

`--dry-run` prints the title, the template, the evidence, and any required sections that are still empty, then a
`---` line and the body, so `sed '1,/^---$/d'` keeps only the body.

```bash
dev pr create --dry-run          # title, template, evidence, which required sections are still empty, the body
dev pr create --edit             # edit the pre-filled body in $EDITOR, then open the PR
dev pr create --body-file body.md
dev pr create --draft            # an early draft; empty sections allowed, no evidence when drafts run no CI
```

AI attribution is removed from the body. If the branch already has an open PR, its address is printed instead.

### Follow, review, and merge

| Command | What it does |
| --- | --- |
| `dev pr view [n]` | One line: state, branches, checks, review, conflicts; then size, author, age, and address. `--body` adds the description |
| `dev pr ls` | Your PRs and the ones waiting for your review. `--all` adds the others; `--all-projects` covers every project (from the registry cache) |
| `dev pr checks [n] [--watch]` | Pass, fail, and pending counts; each failing job with the relevant lines of its log. `--watch` waits for the result and prints only that |
| `dev pr diff [n]` | Files changed with +/−, largest first; `--patch` for the full diff, `--name-only` for the names |
| `dev pr ready [n]` | Checks the title, the required sections, and the local CI evidence for HEAD, then marks a draft ready for review (its one CI run) |
| `dev pr comment [n] -m "…"` · `dev pr review [n] --approve \| --request-changes \| --comment` | Comment or review; `--request-changes` and `--comment` need a message. Attribution is removed |
| `dev pr merge [n]` | Merges when the gate below allows it |

`dev pr checks` exits with `0` when every check passed, `1` when one failed, and `8` while checks are pending. With
`--watch`, it waits up to two minutes for the checks of a fresh push to appear, then waits for the result (90 minutes
at most). A failing job's log is cut to the failing step: its error lines first, then its last lines.

`dev pr merge` refuses, with every reason at once, when the PR is a draft, its checks failed or are still running (use
`--auto` to let GitHub merge when they pass), no checks were reported although workflows run on pull requests, changes
were requested, an approving review is required, it conflicts with the base or is behind it, or branch protection
blocks it.

It uses the method you ask for (`--squash`, `--merge`, `--rebase`) when the repository allows it, otherwise the only
allowed method, otherwise your default. A squash commit gets the PR title and the "What and why" paragraph with the
`Closes #n` lines, never attribution. The merge applies only to the commit you checked (`--match-head-commit`).
Afterward devkit deletes the remote branch (unless `--keep-branch`). When the merge test finds the branch merged, it
also removes the branch's worktree if that is clean and idle, or switches this checkout to the default branch and
fast-forwards it, and deletes the local branch; uncommitted changes, a commit the PR did not carry, or ignored files
that no tool rebuilds keep it, and a warning line of its own says why. Run inside the branch's own worktree, it leaves that worktree and names `dev wt rm <branch>`.
See [Close merged branches](#close-merged-branches). `--dry-run` shows the method and the squash message.

## CI runs

```bash
dev run ls                         # recent runs of this branch, on any host
dev run view --failed              # the latest run: failing jobs, the failing step, its trimmed log
dev run rerun 4711 --flaky "PaymentIT times out on the runner; passed 5 times locally, unrelated to this change"
```

`dev run ls` reads GitHub, GitLab, Bitbucket, and Azure like
[`dev ci status`](commands.md#dev-ci-status---watch---branch-name----all-branches---all-jobs---limit-n---json);
`dev run view` and `dev run rerun` work on GitHub. Re-running a failure without a change repeats it and costs a full
CI cycle, so `dev run rerun` needs `--flaky` with the test and the evidence. devkit records it in
`~/.local/state/devkit/flaky.jsonl` and in the project's session log in the handoff repository, then re-runs the
failed jobs (`--all-jobs` for all of them).

## Releases

`dev release` tags and publishes a release from the default branch, by the project's
[delivery policy](delivery.md#the-delivery-policy):

1. A project whose policy releases with release-please or semantic-release is refused: merge that tool's release PR.
2. You are on the default branch, with a clean tree, and exactly in step with `origin`.
3. The version is `--version`, or the next CalVer version. A project with another scheme (recorded in its policy, or
   visible in its tags and version files) needs `--version X.Y.Z`. The tag must not exist yet.
4. `VERSION`, `package.json`, and `pyproject.toml` say the same version; if not, bump them on the release branch first.
5. HEAD has local CI evidence (or `--ci-passed`), and its CI on GitHub is not failing or still running.
6. The notes are `--notes-file`, or the version's section of `CHANGELOG.md` (or `CHANGES.md`, `HISTORY.md`), or
   GitHub's generated notes.

Then it creates the annotated tag `v<version>` (signed with `--sign` or when git's `tag.gpgSign` is on), pushes it,
and creates the GitHub release (`--draft` and `--prerelease` are passed on). It ends by asking you to download the
published artifact, install it, and smoke-test it. `--dry-run` prints every step and the evidence without doing any of
them. On other hosts it tags and pushes, and leaves the release page to you.

Once the release is published, it deletes the branches it merged, remote and local, and names each with how it was
merged (a draft release leaves them until you publish it). `--dry-run` lists them. See
[Close merged branches](#close-merged-branches).

Because the evidence follows the files, a squash merge of a tested branch that was up to date with `main` keeps its
evidence on `main`. Otherwise, run `dev ci --run` on `main` before you release.

## Issues

`dev issue ls` lists open issues (`--mine`, `--label`, `--state`, `--limit`), `dev issue view <n>` shows one
(`--comments` adds the last five), and `dev issue create --title "…" [--body "…" | --body-file FILE]` opens one with
attribution removed.

## Raw git and gh through `dev`

When you need a git or gh command devkit has no wrapper for, run it through `dev git …` or `dev gh …`. The same rules
apply, then the command runs with your terminal attached.

| Command | Checks |
| --- | --- |
| `dev git stash …` | Refused, except reading commands such as `list` and `show`: use `dev commit --wip` |
| `dev git push …` | The `dev push` rules for any refspec: no plain force push, nothing to the default branch, evidence for each pushed branch (`--ci-passed` accepted) |
| `dev git commit …` | The commit-message and branch rules, then the guard's attribution and secret checks |
| `dev git rebase -i`, `add -p`, and other interactive commands | Refused without a terminal |
| `dev gh pr create …` | Evidence (not for a draft where drafts run no CI), a title in the repository's convention, and the required sections unless `--draft` |
| `dev gh pr merge …` | `--admin` refused; the `dev pr merge` gate; a title in the repository's convention for a squash |
| `dev gh release create …` | A clean default branch and local CI evidence |
| Anything else | The guard's rules |

Put git options before the subcommand after `--` (`dev git -- -c core.pager=cat log`), or use `-C DIR`.

## What works where

| Command | GitHub | GitLab, Bitbucket, Azure, other hosts |
| --- | --- | --- |
| `dev branch`, `dev commit`, `dev rebase`, `dev push`, `dev ci --run` | Yes | Yes (`dev branch ls` and `clean` show PR states only on GitHub) |
| `dev run ls` | Yes | Yes |
| `dev pr`, `dev run view`, `dev run rerun`, `dev issue` | Yes | No: refused with the CLI to use instead (`glab` for GitLab) |
| `dev release` | Tag, push, and GitHub release | Tag and push |

GitHub Enterprise hosts count as GitHub when they are listed in `gitx.github_hosts` (see
[Configuration](configuration.md#git-and-github)).

## How the guard backs it up

The guard sends agents to these commands. A raw `gh pr merge` or `gh release create` is refused with the `dev`
command that checks CI, reviews, and the policy first (prefix `DEVKIT_RAW_GH=1` to bypass it); `gh pr merge --admin`
is always refused; a bare `git stash` is refused; and a commit, push, or tag chained after a pipe is refused unless
`pipefail` is set. The full list is in [Guard rules](guard.md#github-cli).

See also [Delivery](delivery.md), [Templates › Pull request description](templates.md#pull-request-description), and
[Commands › Git and GitHub](commands.md#git-and-github).
