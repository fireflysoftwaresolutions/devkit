# Commands

`dev` with no arguments shows the dashboard. `dev help` (or `dev -h`) lists every command; `dev <command> -h` shows
its options. A `<query>` is a project id, a project name, a path or a substring of an id; when several projects
match and you are in a terminal, an fzf picker opens.

Commands that fail print a reason on stderr and exit non-zero: `2` for usage problems (unknown or ambiguous
project, bad argument), `1` for operations that did not complete, `130` when you cancel a picker or press Ctrl-C.

## Dashboard

### `dev`

Greeting, the project you are in (if any), the workspace summary (projects, worktrees, live sessions, handoff repo,
last sync, canary rate, recently active projects), up to six items that need attention and a tip.

## Projects

### `dev ls [--offline] [--names]`

Table of every project: id, stack, branch, state (modified/untracked files, ahead ↑ / behind ↓), number of worktrees,
open PRs, last activity (● when a session is live), size. Ids marked `*` are not at their canonical path.

| Option | Effect |
|---|---|
| `--offline` | Do not query GitHub; use cached PR data |
| `--names` | Print only project ids, one per line |

### `dev go <query>`

Print a project's path. Used by the `dcd` shell function.

```bash
cd "$(dev go widget)"      # what `dcd widget` does
```

### `dev status [<query>]`

Path, remote, stack, detected commands, branch and state, PR, last commit, worktrees, open PRs, handoff note and
the attention items for the current project (or `<query>`). Outside a project it shows the dashboard.

### `dev clone <owner/repo | host/group/repo | url>`

Clone into the canonical path and print it. GitHub shorthands go through `gh repo clone`; everything else through
`git clone`. The new repository is marked trusted in Codex and gets a handoff note.

```bash
dev clone acme/widget                          # → ~/Development/acme/widget
dev clone git@gitlab.com:group/sub/svc.git     # → ~/Development/gitlab.com/group/sub/svc
dev clone gitlab.acme.io/team/api              # → ~/Development/gitlab.acme.io/team/api (https)
```

If the path already exists it prints the path, warns and exits 0.

### `dev new <name> [--github [--owner OWNER] [--public]] [--no-git]`

Without `--github`: create `~/Development/_local/<name>` with `git init -b main` (or a plain folder with
`--no-git`). With `--github`: create `OWNER/<name>` on GitHub (private unless `--public`, with a README), then clone
it into the canonical path.

### `dev adopt <path>`

Move a repository that is not at its canonical path:

1. refuses (exit 3) while any process works in the repository or its worktrees — close those sessions and shells;
2. moves worktrees placed outside the repository to `~/Development/.worktrees/<project id>/<name>` (`git worktree move`);
3. moves the main checkout (with any nested `.claude/worktrees`) to the canonical path and runs `git worktree repair`;
4. moves the matching Claude Code history folders in `~/.claude/projects` so `claude --resume` keeps working;
5. trusts the new path in Codex, logs the move, and syncs.

Claude Desktop and Codex app conversations opened at the old path should be restarted from the new path.

### `dev open [<query>]`

Open the project in the configured `editor` (default `code`).

## Agents

### `dev claude [<query>] [-w BRANCH] [-- ARGS…]` · `dev codex [<query>] [-w BRANCH] [-- ARGS…]`

Start Claude Code or Codex in a project (default: the current one). With `-w BRANCH` the agent runs in the
managed worktree for that branch, created if needed. Arguments after `--` go to the agent.

```bash
dev claude widget -w feat/search      # Claude Code in ~/Development/.worktrees/<id>/feat-search
dev claude widget -- --resume         # pass arguments to claude
dev codex -- resume --last            # resume the last Codex session in the current project
```

### `dev handoff [<query>] [--edit | --path]`

Print the project's handoff note (after refreshing its auto block). `--path` prints the file path; `--edit` opens it
in `$EDITOR` (then the configured editor, then `vi`).

## Worktrees

### `dev wt new <branch> [--base REF] [--repo QUERY] [--no-deps]`

Create (or reuse) the managed worktree `~/Development/.worktrees/<project id>/<branch>` and print its path. Progress
messages go to stderr.

| Option | Effect |
|---|---|
| `--base REF` | Start point for a new branch (default `origin/<default branch>`) |
| `--repo QUERY` | Project, when you are not inside it |
| `--no-deps` | Do not clone `node_modules` from the main checkout |

### `dev wt ls [--all]`

Worktrees of the current project (or of every project with `--all`): branch, PR, state, location (`canonical`,
`claude`, `codex`, `temp`, `stray`, `missing`), live session or idle time, size, path.

### `dev wt rm <branch | path>`

Remove a worktree only if it has no uncommitted, untracked or unpushed work and no process is using it. The local
branch is deleted when all its commits are on a remote. A registered worktree whose folder is gone is pruned.
Exit 1 when removal was refused.

## Delivery

### `dev ci [<query>] [--commands] [--runs]`

Read `.github/workflows/*.yml` and print each workflow's triggers, jobs, runners, matrices, setup actions and every
`run:` command, so you can run exactly what CI runs before the single push. Expressions (`${{ … }}`) are shown
verbatim.

| Option | Effect |
|---|---|
| `--commands` | Only the `run:` commands, in order (steps that use secrets are omitted) |
| `--runs` | Also list the 8 most recent CI runs (`gh run list`) |

## Tracking and disk

### `dev canary [--days N] [--limit N]`

Instruction-canary report: per agent, how many tracked replies started with `DevKit ·` in the last `N` days
(default 7), then the most recent deviations (default 20) with an excerpt.

### `dev sync [--offline] [--scheduled] [--no-push] [--quiet]`

Rebuild the projects map, `registry.json` and the auto blocks of the handoff notes; commit and push the handoff
repository when something changed.

| Option | Effect |
|---|---|
| `--offline` | No network; cached PR data of any age |
| `--scheduled` | Background mode: refresh PR states and sizes, apply the safe cleanup if `gc.auto` is on, rotate logs |
| `--no-push` | Commit but do not push |
| `--quiet` | No output |

### `dev gc [--apply] [-y] [-v]`

Plan disk reclamation with fresh PR states and print what is reclaimable (dry run). `-v` also lists what is kept and
why. `--apply` asks for confirmation in an interactive terminal (skip with `-y`); in a non-interactive shell it refuses unless you pass `--yes`. It then syncs. Rules: [Disk and cleanup](disk-and-cleanup.md).

### `dev du [--refresh]`

Sizes of every project's main checkout and worktrees, Claude Code transcripts and temp, the Codex home, devkit
temp, npm and Homebrew caches, the Colima VM disk, and `docker system df` when Docker is running. `--refresh`
recomputes cached sizes.

## Environment

### `dev doctor`

Checks the toolchain, GitHub authentication and git identity, the workspace (PATH, shell, config, handoff repo,
layout), the generated instructions and skills, Claude Code (permission mode, attribution, hooks, plugins, MCP),
Codex (approval and sandbox, hook trust, MCP, plugins), Docker, the background job, the instruction canary and free
disk. Each failure prints the fix. Exit 1 if any check fails.

### `dev install [--only STEPS]`

Apply the configuration (idempotent). `STEPS` is a comma list of
`config,cli,git,handoff,instructions,skills,claude,codex,docker,launchd`. See
[Getting started](getting-started.md#what-dev-install-configures).

### `dev update`

`git pull --ff-only` in the devkit repository, then `dev install`.

### `dev docker up | down | status | prune | autostart-on | autostart-off`

| Action | Effect |
|---|---|
| `up` | Start Colima with the configured resources (created on first run) and print the Docker server summary |
| `down` | `colima stop` |
| `status` | `colima status` and `docker system df` |
| `prune` | `docker system prune -f` and `docker builder prune -f` (never volumes) |
| `autostart-on` / `autostart-off` | Start Colima at login with `brew services` (or stop doing so) |

### `dev version`

Version, install path and copyright.

## Shell integration

`dev install` makes `~/.zshrc` source `shell/devkit.zsh`, which adds:

| Function | Effect |
|---|---|
| `dcd <query>` | `cd` into a project (fzf picker when several match) |
| `dwt <branch> [--base REF]` | Create or reuse a managed worktree and `cd` into it |

It also puts `~/.local/bin` on the PATH, sets the Docker and telemetry environment variables, and loads zsh
completion for `dev` (commands, project ids, worktree branches, docker actions, install steps) and `dcd`.

## Internal commands

`dev hook <event> --agent claude|codex` is the hook entry point called by Claude Code and Codex
([Agents](agents.md#hooks)). `dev __complete <projects|branches|commands>` feeds shell completion.

See also: [Concepts](concepts.md) · [Configuration](configuration.md)
