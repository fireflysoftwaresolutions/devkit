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
dev commit -a -m "feat(invoice): add CSV export" --body "Finance copies invoices by hand every month."
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

`dev branch ls` lists the local branches with their upstream (ahead ↑, behind ↓, or `gone`), how far each is from
`origin/<default>`, its PR, its age, and the worktree that has it checked out.

`dev branch clean` lists the local branches that can go: merged into the default branch, whose PR was merged at the
same commit, or gone upstream with every commit on the remote. It keeps the current branch, permanent branches,
branches checked out in a worktree, and branches with commits that exist only here, and says why. Nothing is deleted
until you add `--apply`.

## Commits

`dev commit -m "type(scope): summary"` commits the staged changes, every tracked change with `-a`, or only the paths
you list. More `-m` options add paragraphs, and `--body` adds the reason.

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
| Steps that need CI secrets or write CI plumbing (`$GITHUB_ENV`, `$GITHUB_OUTPUT`) | They only make sense on the runner |
| Jobs that deploy or publish, run on Windows, or only on other events | They are not part of the check of a change |

A step that could be a real check but cannot run here (an unknown condition, an unresolved `${{ }}` expression, a
shell other than bash, a reusable workflow, a missing working directory) means no evidence is recorded. Run it
yourself, then record the result with `dev ci --mark-passed --note "<command>: <result>"`.

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
  branch (`release/26.11.4` becomes `chore(release): 26.11.4`). It must be a Conventional Commit, because a squash
  merge makes it the commit on `main`.
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
Afterward devkit deletes the remote branch (unless `--keep-branch`), switches to the default branch, fast-forwards it,
and deletes the local branch, unless a worktree or uncommitted changes still need it. `--dry-run` shows the method and
the squash message.

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
| `dev gh pr create …` | Evidence (not for a draft where drafts run no CI), a Conventional title, and the required sections unless `--draft` |
| `dev gh pr merge …` | `--admin` refused; the `dev pr merge` gate; a Conventional title for a squash |
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
