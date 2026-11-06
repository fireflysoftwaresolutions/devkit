# Disk and cleanup

Agents fill disks with worktrees, duplicated dependencies, build output and temp files. devkit reclaims that space
with `dev gc`, applied automatically every hour for the safe cases, and never deletes work that is not already safe
on a remote.

## `dev gc`

```bash
dev gc            # dry run: what is reclaimable and why (PR states refreshed from GitHub)
dev gc -v         # also list what is kept, and why
dev gc --apply    # reclaim it (asks for confirmation in a terminal; -y to skip)
```

### What it reclaims

| Action | When |
|---|---|
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

- never touch a checkout with uncommitted changes, untracked files, or commits that exist on no remote;
- never touch a checkout where any process works (agent session, shell, editor, dev server), detected from every
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

- PR state is read through `gh`, so only GitHub repositories benefit from the PR rule. On GitLab, Bitbucket and other
  hosts, a worktree is removed only when its commits are already contained in the default branch (merge commits and
  fast forwards; squash merges are not detected) — remove the others with `dev wt rm`.
- Activity is measured from git metadata and Claude Code transcripts; editing files for days without any git
  operation in an idle worktree may look idle, but uncommitted changes always keep it.

## Automatic cleanup

The launchd job `com.devkit.sync` runs `dev sync --scheduled` every hour. With `gc.auto` on (the default) it applies
the same plan as `dev gc --apply`. Set `"gc": {"auto": false}` in `~/.config/devkit/config.json` to only report.

## `dev du`

```bash
dev du             # cached sizes (refreshed hourly)
dev du --refresh   # recompute now
```

Main checkout and worktree sizes per project, Claude Code transcripts and temp, the Codex home, devkit temp, npm and
Homebrew caches, the Colima VM disk, and `docker system df`.

## What the agents keep themselves

| Data | Retention |
|---|---|
| Claude Code transcripts, file history, plans, tool results | Deleted after `cleanupPeriodDays` = 14 (set by devkit; Claude Code's default is 30) |
| Claude Code worktrees | Clean, unnamed session worktrees are removed by Claude Code at exit; the rest by `dev gc` |
| Codex app worktrees | Managed by the app: it keeps the 15 most recent (Settings → Worktrees), never deletes pinned or running chats; devkit never removes them |
| Codex sessions | No automatic retention; use `codex archive` / `codex delete` |

## Saving disk up front

- Worktrees reuse `node_modules` copy-on-write when the lockfile matches (APFS clones cost almost nothing).
- Prefer `pnpm` and `uv` for new projects: shared stores and linked installs.
- The global instructions forbid reinstalling dependencies "just in case", backup copies, and scratch files in
  repositories.

See also: [Concepts › Worktree lifecycle](concepts.md#worktree-lifecycle) · [Docker](docker.md)
