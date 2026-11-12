# Disk use

This guide explains what `dev du` measures on your Mac, which of it is safe to delete, why Colima's disk takes more
space than Docker reports, and how `dev free` deletes plan items without touching work that is not committed or
pushed.

## What `dev du` measures

`dev du` answers one question first: what fills your disk, why, and what you can safely free. It looks only at what
is on this Mac; projects that your portfolio knows but that are not checked out here are counted in a footer
(`dev ls --all` lists them).

| Part | What devkit measures |
| --- | --- |
| Each project | The main checkout, managed worktrees (`~/Development/.worktrees`), Claude Code worktrees (`.claude/worktrees`), Codex worktrees, and copies outside devkit's folder layout, which are flagged with `!` |
| Each checkout | Its size, branch or pull request (PR) state, commits to push, changed files, and last activity, or "in use" when a program works in it |
| Largest folders | The folders git ignores, at any depth, each measured with `du` and named by kind: build output (`dist`, `target`, `.next`, …), dependencies (`node_modules`, `.venv`, and any folder with a `pyvenv.cfg`), caches (any folder with a `CACHEDIR.TAG`), or other ignored files |
| Colima | The size of its disk on the Mac, what Docker holds in it (`docker system df`), and the free space inside the virtual machine that macOS has not got back |
| Agent data and caches | Claude Code chats and temp files, Codex chats and app data, the Homebrew, npm, pnpm, and uv caches, Playwright browsers, and devkit's scratch files |

A project gets a "Why so big" note when its worktrees and copies take more than twice its main checkout: each one
is a full checkout with its own build output and dependencies.

```bash
dev du                    # the whole report, from the cache
dev du kit                # one project, by alias or part of its name
dev du --plan             # only the plan to free space
dev du --refresh          # measure again now
dev du --json             # the measurement and the plan, for the dashboard and scripts
```

### Measurements are cached

Measuring every folder takes from a few seconds to a few minutes, so `dev du` saves each measurement with its time and
shows it from the cache in well under 2 seconds ("measured 3 min ago"). It measures again by itself when the cache is
older than 12 hours, and whenever you pass `--refresh`. The checkout sizes it measures also update the sizes that
`dev ls` and the dashboard show.

## What is safe to delete

Every plan item has one of three safety words, and they always mean the same thing.

| Word | Meaning | `dev free` |
| --- | --- | --- |
| `safe` | Nothing is lost: it is merged, rebuilt by the next build, or downloaded again when needed | Deletes it after you say yes |
| `check first` | It may hold work: a program is working in it, it has changed files, or it has commits that are not pushed | Refuses until the reason is gone, and says what to do |
| `keep` | Not in the plan; listed with the reason, such as your chat history | Never |

| Kind | Safety | What happens afterwards |
| --- | --- | --- |
| Dependency folders and tool caches in a checkout (`node_modules`, `.venv`, `.next`, `__pycache__`, …) | `safe` | A tool fills them again on its own; a Rust or Next.js build takes a few minutes. Changed files stay |
| Generic build output (`build`, `dist`, `target`, `out`, `coverage`, …) | `check first` | It may hold files no build step wrote, such as release evidence. Look inside, then delete it yourself; `dev free --safe` skips it |
| A merged worktree or copy with no changes and nothing to push | `safe` | It is removed with `git worktree remove`; its branch stays on the remote |
| A merged worktree with ignored files that no tool rebuilds | `check first` | It is kept whole; the reason names those paths with their sizes. Move them out or delete them, then `dev wt rm <path>` |
| A Claude Code worktree that is merged | `safe` | Its chats stay, but those sessions cannot continue in that folder |
| Docker images that no container uses | `safe` | A tool that needs one downloads it again |
| Homebrew downloads, npm cache, pnpm store, uv cache, Playwright browsers | `safe` | Downloaded again when needed |
| A checkout a program is working in, or with changes or commits to push | `check first` | Commit, push, or close the program, then run `dev free` again |
| Claude Code chats, Codex chats, and Codex app data | `keep` | Used to resume sessions |
| Claude Code temp files and devkit scratch files | Listed, not in the plan | macOS empties temp files at restart; scratch files belong to running sessions |
| Docker volumes | Never in the plan | They hold your containers' data |
| Codex worktrees | Never removed | The Codex app manages them |

## Colima's disk

Colima runs Docker in a small Linux virtual machine. Its disk is one sparse file that can grow to the size you set
(80 GB by default) and takes on your Mac only the space it has used.

When Docker deletes images or containers, the space is free inside the virtual machine, but the file on your Mac does
not shrink: macOS gets the space back only when the virtual machine tells it which blocks are free. `dev du` shows
the difference between the file's size on your Mac and what the virtual machine uses as space "Docker already freed
in the virtual machine, which macOS has not got back." `dev docker trim` returns it: it runs `fstrim` inside each running Colima profile and
reports how much macOS got back. It deletes nothing, and Docker keeps running.

```bash
dev docker trim           # return the Colima free space to macOS (Colima must be running)
```

Unused images are one plan item that runs `docker image prune -a -f` and then the trim. devkit never prunes volumes.

## Free space

1. Run `dev du` (or `dev du --plan`) and read the plan. Each item shows what it frees, its safety word, what happens
   afterwards, and the command; when devkit runs another tool's command, the item says which one.
2. Free the items you want, by number:

   ```bash
   dev free 2 4              # those items of the plan you just saw
   dev free --safe           # every item marked safe
   ```

3. `dev free` prints exactly what it deletes and the space, and asks `Delete it? [y/N]`.
4. At that moment it checks safety again: changed files, commits that are not pushed, and a program working in the
   folder. A refusal says why and what to do, for example
   `✗ Not freed: kit-consulting-ci has 4 changed files. Commit or discard them first.`
5. It reports what it freed (`✓ Freed 6.4 GB.`) and records each deletion in the audit trail and in the handoff
   repository's activity log.

A number always means the item you saw. `dev du` saves the plan with its time; if a newer measurement changed the plan
since then, `dev free` prints the new plan and deletes nothing. `dev free` without numbers prints the plan.

`dev gc` is the same command under its old name. `dev gc --apply` still runs the unattended cleanup that the nightly
`gc` job uses (see [Disk and cleanup](disk-and-cleanup.md)).

See also [Commands › Disk](commands.md#dev-du-name---plan---refresh---json), [Docker](docker.md), and
[Disk and cleanup](disk-and-cleanup.md).
