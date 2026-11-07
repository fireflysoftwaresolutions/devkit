# Disk and cleanup

Agents fill disks with worktrees, duplicated dependencies, build output, and temp files. devkit reclaims that space
with an unattended cleanup that the nightly `gc` job applies for the safe cases, and never deletes work that is not
already safe on a remote. To see what fills your disk and free it yourself, item by item, use `dev du` and
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
| Remove worktree | Linked worktree, not a Codex app worktree, clean, nothing unpushed, idle longer than `gc.grace_hours` (24), and either its PR is merged or closed, or its branch has no commits beyond `origin/<default>` |
| Prune build output | Worktree idle longer than `gc.idle_artefact_days` (7) and clean: deletes regenerable folders that git reports as ignored |
| Purge temp | Entries in Claude Code's temp root (`/tmp/claude-<uid>/`) and `~/Development/.tmp` untouched for `gc.temp_days` (7), except folders of projects where a process is working |
| Prune metadata | A registered worktree whose folder no longer exists (`git worktree prune`) |
| Docker | Dangling images and build cache older than 7 days, when Docker is running and `gc.docker` is on |

Build output folder names: `node_modules`, `.venv`, `venv`, `dist`, `build`, `target`, `.next`, `.nuxt`, `.angular`,
`.turbo`, `.parcel-cache`, `.cache`, `coverage`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `__pycache__`,
`.gradle`, `.build`, `DerivedData`, `.output`, `.svelte-kit`, `storybook-static`, `.nx`, and `vendor` when a
`composer.json` sits next to it. Only folders that git lists as ignored are considered.

### Safety rules

Checked when planning **and again right before acting**:

- never change a checkout with uncommitted changes, untracked files, or commits that exist on no remote;
- never change a checkout where any process works (agent session, shell, editor, dev server), detected from every
  process's working directory;
- never remove main checkouts; never remove Codex app worktrees;
- before removing a worktree because of its PR, the PR state is fetched again from GitHub and must be merged or
  closed;
- `git worktree remove` is used (never `rm -rf`), so git metadata stays consistent; the local branch is deleted
  only when all its commits are on a remote;
- every action is logged in the handoff repository (`log/YYYY-MM.md`).

Kept items are reported with their reason: active session, locked, uncommitted work, unpushed commits, open PR,
work in progress.

### Limitations

- PR state is read through `gh`, so only GitHub repositories benefit from the PR rule. On GitLab, Bitbucket, and other
  hosts, a worktree is removed only when its commits are already contained in the default branch (merge commits and
  fast forwards; squash merges are not detected); remove the others with `dev wt rm`.
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
