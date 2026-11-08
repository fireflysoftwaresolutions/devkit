# Disk and cleanup

Agents fill disks with worktrees, duplicated dependencies, build output, and temp files. devkit reclaims that space
with an unattended cleanup that the nightly `gc` job applies for the safe cases, and never deletes work that is not
already safe on a remote or merged into the default branch. To see what fills your disk and free it yourself, item by item, use `dev du` and
`dev free` (see [Disk use](disk.md)).

## The unattended cleanup

`dev gc` without `--apply` is `dev free` under its old name. With `--apply`, it runs the cleanup below:

```bash
dev gc -v         # dry run: what the cleanup would reclaim, what it keeps, and why (PR states from GitHub)
dev gc --apply    # reclaim it (asks for confirmation in a terminal; -y to skip)
```

### What it reclaims

| Action | When |
| --- | --- |
| Remove worktree | Linked worktree, not a Codex app worktree, clean, idle longer than `gc.grace_hours` (24), and either its branch is merged (see [Merged branches](#merged-branches)), or nothing is unpushed and its PR is merged or closed or its branch has no commits beyond `origin/<default>` |
| Delete merged branch | A merged local branch without a worktree that has not moved for `gc.grace_hours`, and a merged remote branch whose PR merged or that devkit pushed as a backup (see [Merged branches](#merged-branches)) |
| Prune build output | Worktree idle longer than `gc.idle_artefact_days` (7) and clean: deletes regenerable folders that git reports as ignored |
| Purge temp | Entries in Claude Code's temp root (`/tmp/claude-<uid>/`) and `~/Development/.tmp` untouched for `gc.temp_days` (7), except folders of projects where a process is working |
| Prune metadata | A registered worktree whose folder no longer exists (`git worktree prune`) |
| Docker | Dangling images and build cache older than 7 days, when Docker is running and `gc.docker` is on |

Build output folder names: `node_modules`, `.venv`, `venv`, `dist`, `build`, `target`, `.next`, `.nuxt`, `.angular`,
`.turbo`, `.parcel-cache`, `.cache`, `coverage`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `__pycache__`,
`.gradle`, `.build`, `DerivedData`, `.output`, `.svelte-kit`, `storybook-static`, `.nx`, and `vendor` when a
`composer.json` sits next to it. Only folders that git lists as ignored are considered.

### Merged branches

The cleanup closes branches with the merge test of `dev branch prune`: a branch is merged when its tip is in
`origin/<default>`, when its pull request from this repository merged, or when every commit has an equivalent there
(see [Git and GitHub › Close merged branches](git-and-github.md#close-merged-branches)). What `dev branch prune`
never deletes, the cleanup keeps too: the default and permanent branches, the current branch, a branch with an open
pull request, and an embargoed branch.

- An idle, clean worktree whose branch is merged goes with its branch, even when its commits are on no remote.
- A merged local branch without a worktree goes once it has not moved for `gc.grace_hours` (24): its newest reflog
  entry, or else its last commit, is older than that.
- A merged remote branch goes only when its pull request merged or devkit pushed it as a backup (`dev backup`,
  `dev sync`). Anyone else's merged branch on `origin` is listed as kept, with `dev branch prune --apply` to delete
  it. Off GitHub, remote branches stay: devkit does not read the host's merge requests, and an open one would lose
  its branch.

`dev free` uses the same merge test, so a worktree whose branch was merged by squash or cherry-pick counts as merged,
not as unpushed.

When the `gc` job is not installed, the hourly `dev sync --scheduled` applies this cleanup itself (with `gc.auto`
on). With `--offline` or `--no-push`, that run keeps every branch on `origin` and says so:
`kept remote branch …: this run does not reach origin`.

### Safety rules

Checked when planning **and again right before acting**:

- never change a checkout with uncommitted changes or untracked files, or with commits that exist on no remote
  unless the merge test finds its branch merged;
- never change a checkout where any process works (agent session, shell, editor, dev server), detected from every
  process's working directory;
- never remove main checkouts; never remove Codex app worktrees;
- before removing a worktree because of its PR, the PR state is fetched again from GitHub and must be merged or
  closed;
- `git worktree remove` is used (never `rm -rf`), so git metadata stays consistent; the local branch is deleted
  only when all its commits are on a remote or the merge test finds it merged at the commit the plan saw;
- a remote branch is deleted with a lease on the commit the plan saw, so a branch someone pushed to since stays;
- every action is logged in the handoff repository (`log/YYYY-MM.md`), and a merged branch deleted without a
  worktree is also audited as `branch.pruned`.

Kept items are reported with their reason: active session, locked, uncommitted work, unpushed commits, open PR,
work in progress, a merged branch that moved recently, and a merged remote branch that devkit did not push.

### Limitations

- PR state is read through `gh`, so only GitHub repositories benefit from the PR rule. On GitLab, Bitbucket, and other
  hosts, a branch counts as merged only by ancestry or by content (merge commits, fast-forwards, cherry-picks, and
  rebase merges; a squash merge of more than one commit is not detected); remove the others with `dev wt rm`.
- Activity is measured from git metadata and Claude Code transcripts; editing files for days without any git
  operation in an idle worktree may look idle, but uncommitted changes always keep it.

## Automatic cleanup

The scheduled `gc` job runs `dev gc --apply --yes --auto-only` every night at 04:00: it applies the actions of the
cleanup that are safe without a person. Set `"gc": {"auto": false}` in `~/.config/devkit/config.json` (the
wizard's "report only") to keep the job off, or move it with `dev jobs set gc --at 02:00`. `dev jobs show gc` lists
its recent runs, and every action is in the audit trail (see [Scheduled jobs and the audit trail](jobs-and-audit.md)).

The `sleep` job (03:30) trims devkit's own records as well: old timing records, finished runs, session records,
caches, the hooks log, and old `dev rm` manifest entries (see [Lessons › Sleep](lessons.md#sleep)).

## What fills the disk

`dev du` explains what takes space on this Mac and why, and ends with a plan to free it; `dev free <n>` deletes an
item of that plan after asking. Both are described in [Disk use](disk.md).

## What the agents keep themselves

| Data | Retention |
| --- | --- |
| Claude Code transcripts, file history, plans, tool results | Deleted after `cleanupPeriodDays` = 14 (set by devkit; Claude Code's default is 30) |
| Claude Code worktrees | Clean, unnamed session worktrees are removed by Claude Code at exit; the merged ones by the cleanup or `dev free` |
| Codex app worktrees | Managed by the app: it keeps the 15 most recent (Settings → Worktrees), never deletes pinned or running chats; devkit never removes them |
| Codex sessions | No automatic retention; use `codex archive` / `codex delete` |

## Deleting by hand

`dev rm` moves what you delete to the macOS Trash instead of unlinking it, so it can be restored with `dev rm --undo`
(see [Commands › Files](commands.md#files)). The space comes back only when the Trash is emptied; neither the cleanup
nor `dev free` empties it. Gitignored build output deleted with `dev rm -r` needs no flag.

## Saving disk up front

- Worktrees reuse `node_modules` copy-on-write when the lockfile matches (APFS clones cost almost nothing).
- Prefer `pnpm` and `uv` for new projects: shared stores and linked installs.
- The global instructions forbid backup copies, scratch files in repositories, and reinstalling dependencies
  "just in case."

See also [Concepts › Worktree lifecycle](concepts.md#worktree-lifecycle) and [Docker](docker.md).
