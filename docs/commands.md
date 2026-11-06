# Commands

This page lists every `dev` command and option. `dev help` (or `dev -h`) prints the grouped list, and
`dev <command> -h` prints the options of one command.

A `<query>` is a project id, a project name, a path, or part of an id. When more than one project matches and you are
in a terminal, an fzf picker opens.

Commands that fail print a reason on stderr and exit with a non-zero code: `2` for usage problems (an unknown or
ambiguous project, a bad argument, a missing `--why`), `1` for an operation that did not complete, and `130` when you
cancel a picker or press Ctrl-C.

## Home

### `dev`

Prints a greeting, the project you are in (if any), the workspace summary (projects, worktrees, live sessions, the
handoff repository, the last sync, the canary rate, recently active projects), up to six items that need attention,
and a tip.

## Projects

### `dev ls [--offline] [--names]`

A table of every project: id, stack, branch, state (modified and untracked files, ahead ↑ and behind ↓), number of
worktrees, open PRs, last activity (● when a session is live) and size. Ids marked `*` are not at their canonical path.

| Option | Effect |
| --- | --- |
| `--offline` | Do not query GitHub; use cached PR data |
| `--names` | Print only the project ids, one per line |

### `dev go <query>`

Prints a project's path. The `dcd` shell function uses it.

```bash
cd "$(dev go widget)"      # what `dcd widget` does
```

### `dev status [<query>]`

The current project, or `<query>`: path, remote, stack, detected commands, branch and state, PR, last commit,
worktrees, open PRs, the handoff note, the latest status block drawn as a block (see
[Concepts › The status block](concepts.md#the-status-block)) and the items that need attention. Outside a project it
shows the home screen.

### `dev clone <owner/repo | host/group/repo | url>`

Clones into the canonical path and prints it. GitHub shorthands go through `gh repo clone`; everything else goes
through `git clone`. The new repository is marked trusted in Codex and gets a handoff note.

```bash
dev clone acme/widget                          # → ~/Development/acme/widget
dev clone git@gitlab.com:group/sub/svc.git     # → ~/Development/gitlab.com/group/sub/svc
dev clone gitlab.acme.io/team/api              # → ~/Development/gitlab.acme.io/team/api (https)
```

If the path already exists, it prints the path, warns, and exits with `0`.

### `dev new <name> [--github [--owner OWNER] [--public]] [--no-git]`

Without `--github`, creates `~/Development/_local/<name>` with `git init -b main` (or a plain folder with `--no-git`).
With `--github`, creates `OWNER/<name>` on GitHub (private unless `--public`, with a README) and clones it into the
canonical path. `--owner` defaults to you.

### `dev adopt <path>`

Moves a repository that is not at its canonical path:

1. Refuses (exit `3`) while any process works in the repository or its worktrees. Close those sessions and shells
   first.
2. Moves worktrees placed outside the repository to `~/Development/.worktrees/<project id>/<name>`
   (`git worktree move`).
3. Moves the main checkout (with any nested `.claude/worktrees`) to the canonical path and runs
   `git worktree repair`.
4. Moves the matching Claude Code history folders in `~/.claude/projects`, so `claude --resume` keeps working.
5. Trusts the new path in Codex, logs the move, and syncs.

Restart Claude Desktop and Codex app conversations that were opened at the old path from the new path.

### `dev open [<query>]`

Opens the project in the configured `editor` (default `code`).

## Portfolio

These commands read and write the handoff repository. [Teams and machines](teams.md),
[Initiatives](initiatives.md), and [Progress and communication](progress-and-communication.md) explain the model
behind them.

### `dev dashboard [--no-open] [--path]`

Regenerates the HTML command center and opens it in your browser. `--no-open` regenerates and prints the path;
`--path` prints the path only. See [Dashboard](dashboard.md).

### `dev overview [--json] [--days N] [--all]`

The portfolio digest for you and your agents: every initiative (status, priority, progress bar, milestone, due date,
member projects, task counts, Now tasks), then every project (status, priority, live sessions, purpose, the latest
status, Now tasks, open task count, the last session line), then the items that need attention.

| Option | Effect |
| --- | --- |
| `--json` | Machine-readable output for agents and scripts |
| `--days N` | Window for the commit count of each project (default 7) |
| `--all` | Also list archived projects and initiative members that are not cloned on this Mac |

### `dev standup [--days N]`

What happened in the last `N` days (default 1), per project: tasks completed, your commits, session lines, and the
current Now tasks.

### `dev initiative [new | add | rm | ls | show]`

Cross-project initiatives. See [Initiatives](initiatives.md).

| Command | Effect |
| --- | --- |
| `dev initiative` · `dev initiative ls` | Table of initiatives: status, priority, progress, milestone, due date, number of projects, open tasks |
| `dev initiative new <slug> [--title "…"] [--projects a/b,c/d]` | Creates `initiatives/<slug>/README.md` from the initiative template, with one workstream per member project |
| `dev initiative add <slug> <project>` | Adds a member project (and its workstream heading) |
| `dev initiative rm <slug> <project>` | Removes a member project |
| `dev initiative show <slug>` | The initiative: progress and where it comes from, milestone, due date, goal, every member with status and progress, and the Now tasks |

The guard refuses `new`, `add`, and `rm` from inside a project session: membership is a portfolio decision, so run them
from `~/Development` or ask the developer.

### `dev task [-p PROJECT | -i SLUG] [--all] [<verb> …]`

Work items of the project you are in, of another project with `-p`, or of an initiative with `-i`. Without a verb
(or with `ls`) it lists the open items in board order (Now, then Next, then Later; high priority first; oldest first).
The number in front of each item is what the other verbs accept. `--all` lists every done item instead of the last
five.

`-p` and `-i` work before or after the verb. The examples put them after it:

```bash
dev task done 3 -p acme/billing
dev task add "Agree on the event schema" --now -i payments
```

A `<selector>` is an id (`T-…`, `F-…`, `B-…`, `S-…`, or a unique prefix), the number from the list, or a unique part
of the title. See [Teams and machines](teams.md) for locks, versions, and the changelog.

| Verb | Effect |
| --- | --- |
| `add <title> [options]` | Creates a work item (see the next table) |
| `start <selector> [--why]` | Claims the lock (8 hours by default), pushes it at once, and moves a todo item to Now as doing |
| `claim <selector> [--why]` | Same as `start` |
| `release <selector> [--why]` | Gives the lock back; the item returns to todo. Also returns a blocked item to todo |
| `done <selector> [--why]` | Marks the item done and releases the lock |
| `block <selector> --why "…"` | Marks the item blocked |
| `cancel <selector> --why "…"` | Cancels the item and clears its lock |
| `move <selector> --to now\|next\|later --why "…"` | Moves the item between buckets |
| `edit <selector> [--title "…"] [--body-file FILE] [--priority high\|normal\|low] [--due YYYY-MM-DD] --why "…"` | Changes the title, the description, the priority, or the due date |
| `delegate <selector> --to LOGIN [--why "…"] [--due YYYY-MM-DD] [--no-copy]` | Assigns the item to a teammate, releases your lock, prints the message to send, and copies it to the clipboard |
| `show <selector>` | The item's fields, its description, and its history |
| `history <selector>` | The item's changelog only |
| `ls` | The open items (the default) |

Options of `dev task add`:

| Option | Effect |
| --- | --- |
| `--now` · `--next` · `--later` | The bucket (default Next) |
| `--type task\|feature\|bug\|spike` | The kind of work; the id starts with `T-`, `F-`, `B-`, or `S-` and the description uses that type's template |
| `--priority high\|normal\|low` | Priority (default normal) |
| `--due YYYY-MM-DD` | Due date |
| `--why "…"` | Why the work exists; recorded in the changelog and filled into the template |
| `--done "…"` | An acceptance criterion; repeat it for more than one. Fills the template's `Done when` checklist |
| `--claim` | Claims the new item at once |
| `--body "…"` | Your own description instead of the template |
| `--no-template` | No description at all |

`edit`, `move`, `cancel`, and `--why` on the other verbs record the reason in the item's changelog and in its commit;
`edit`, `move`, and `cancel` refuse to run without it. Changing an item that someone else holds an unexpired lock on
is refused; coordinate with them or wait for the lock to expire.

### `dev note set <key=value> … [-p PROJECT | -i SLUG]`

Sets metadata in the frontmatter of a project note (or of an initiative note with `-i`).

| Key | Values |
| --- | --- |
| `status` | `active` · `paused` · `blocked` · `done` · `archived` |
| `priority` | `high` · `normal` · `low` |
| `milestone` | Free text |
| `due` | `YYYY-MM-DD` |
| `tags` | Comma-separated, `tags=payments,api` |
| `progress` | `0`–`100`, a manual value that overrides the progress computed from tasks; empty to compute it again |

Initiatives also accept `title`, `owner`, `projects` (comma-separated) and `source`.

```bash
dev note set status=blocked priority=high
dev note set milestone="Refunds live" due=2026-12-15 -i payments
```

### `dev plan [new | revise | approve | supersede | ls] [-p PROJECT | -i SLUG]`

Versioned plans and specs in `plans/<scope>/`. See [Templates](templates.md#plans-and-specs) and
[Teams and machines › Versioned plans](teams.md#versioned-plans).

| Command | Effect |
| --- | --- |
| `dev plan new "<title>" [--kind plan\|spec\|spike\|bugfix\|adr]` | Creates `plans/<scope>/<date>-<slug>.md` from the template of that kind (default `plan`), at version 1, status draft |
| `dev plan revise <file> --why "…"` | Records a change: bumps the version, logs who, when, why, and the size of the change, and commits. A plan written without devkit (for example by Superpowers) is adopted into the format on its first revision |
| `dev plan approve <file> [--why "…"]` | Sets the status to approved and records a version |
| `dev plan supersede <file> --why "…"` | Sets the status to superseded and records a version |
| `dev plan` · `dev plan ls` | Every plan (or the plans of `-p` / `-i`): id, status, version, title, path, and `✎ needs dev plan revise` when the body changed without a revision |

### `dev team [--hours N]`

Who is working on what: one entry per developer and machine (`user@host`) that published presence in the last `N`
hours (default 24), with its live sessions (agent, project, branch, latest status) and the locks it holds. Each
machine publishes its own presence file when it syncs.

### `dev progress [start | step | block | done | time]`

Live progress of long agent work. See [Progress and communication](progress-and-communication.md).

| Command | Effect |
| --- | --- |
| `dev progress start "<goal>" [--steps N] [--plan "a; b; c"] [--estimate 30m]` | Starts a run for the current session. `--plan` names the steps (shown as a checklist) and sets `--steps` to their number. Prints how long similar runs took here |
| `dev progress step "<what finished>" [--next "…"]` | One step finished; prints the update to send |
| `dev progress block "<why>" [--next "<what is needed>"]` | The run is blocked; prints the update and sends a desktop notification |
| `dev progress done "<outcome>"` | The run is finished; records the time against the estimate and sends a desktop notification |
| `dev progress` | Every run of the last 12 hours on this Mac, and the runs of other machines from their presence |
| `dev progress time [--days N] [-p PROJECT]` | Same as `dev time` |

Estimates accept `45m`, `1h30m`, `2h`, or a number of minutes.

### `dev time [--days N] [-p PROJECT]`

How long things take, over the last `N` days (default 7): turn length (median, p90, longest), how accurate past
estimates were and the factor that corrects new ones, subagent durations, the slowest commands by total time, and the
runs in progress.

### `dev report [--days N] [-i SLUG] [-p PROJECT] [-o FILE]`

A status report in Markdown for the team or for management, built only from the handoff repository: a summary table,
then per initiative its health with the reason, progress, milestone, due date, owner, goal, what was done in the
period and by whom, what is in progress, what is next, blockers, and risks, and who worked on it; then the projects
outside initiatives. `--days` sets the period (default 7), `-i` or `-p` limits it to one initiative or project, and
`-o` writes it to a file. The health rules are in
[Progress and communication › Status reports](progress-and-communication.md#status-reports).

### `dev handoff-import <staging> --slug SLUG [--dry-run]`

Adopts an older handoff or program repository as an initiative, from a prepared staging folder. `--dry-run` prints
what it would import. The staging format is in [Initiatives › Adopt an existing handoff
repository](initiatives.md#adopt-an-existing-handoff-repository).

## Agents

### `dev claude [<query>] [-w BRANCH] [-- ARGS…]` · `dev codex [<query>] [-w BRANCH] [-- ARGS…]`

Starts Claude Code or Codex in a project (default: the current one). With `-w BRANCH` the agent runs in the managed
worktree for that branch, which is created when needed. Arguments after `--` go to the agent.

```bash
dev claude widget -w feat/search      # Claude Code in ~/Development/.worktrees/<id>/feat-search
dev claude widget -- --resume         # pass arguments to claude
dev codex -- resume --last            # resume the last Codex session in the current project
```

### `dev handoff [<query>] [--edit | --path | --log "…" [--agent AGENT]]`

Prints the project's handoff note after refreshing its generated blocks.

| Option | Effect |
| --- | --- |
| `--path` | Prints the note's path |
| `--edit` | Opens the note in `$EDITOR` (then the configured editor, then `vi`) |
| `--log "…"` | Appends a line to the project's session log (`sessions/<project id>/YYYY-MM.md`) with the date, you, your machine and the agent, and commits it |
| `--agent` | Who logs the line: `human` (default), `claude` or `codex` |

## Worktrees

### `dev wt new <branch> [--base REF] [--repo QUERY] [--no-deps]`

Creates (or reuses) the managed worktree `~/Development/.worktrees/<project id>/<branch>` and prints its path. The
branch name must follow the naming rule (see [Guard rules › Branch names](guard.md#branch-names)). Progress messages
go to stderr.

| Option | Effect |
| --- | --- |
| `--base REF` | Start point for a new branch (default `origin/<default branch>`) |
| `--repo QUERY` | The project, when you are not inside it |
| `--no-deps` | Do not clone `node_modules` from the main checkout |

### `dev wt ls [--all]`

The worktrees of the current project (or of every project with `--all`): branch, PR, state, location (`canonical`,
`claude`, `codex`, `temp`, `stray`, `missing`), live session or idle time, size, and path.

### `dev wt rm <branch | path>`

Removes a worktree only when it has no uncommitted, untracked, or unpushed work and no process uses it. The local
branch is deleted when all its commits are on a remote. A registered worktree whose folder is gone is pruned. Exits
with `1` when removal was refused.

## Delivery

### `dev ci [<query>] [--commands] [--runs]`

Reads `.github/workflows/*.yml` and prints each workflow's triggers, jobs, runners, matrices, setup actions and every
`run:` command, so you can run exactly what CI runs before the single push. Expressions (`${{ … }}`) are shown as
written.

| Option | Effect |
| --- | --- |
| `--commands` | Only the `run:` commands, in order (steps that use secrets are left out) |
| `--runs` | Also list the 8 most recent CI runs (`gh run list`) |

### `dev calver [--next]`

The repository's current CalVer tag, the next version (`YY.MM.PATCH`), and the names of its tag and release branch. `--next` prints only the next version. Outside a repository it reads the devkit repository.

### `dev sec scan [<query>] [--history] [--config RULES] [-v]`

Scans the current repository (or `<query>`) for security problems. Each scanner is optional; a missing one is reported
with its install command. Exits with `1` when any scanner reports findings.

| Scanner | Finds |
| --- | --- |
| gitleaks | Secrets in the working tree, and in the git history with `--history` |
| osv-scanner | Known vulnerabilities in lockfiles and manifests |
| Semgrep | Code patterns (rule set `p/default`, or `--config`); metrics are off |
| Trivy | High and critical vulnerabilities, misconfiguration (Dockerfile, Terraform, Kubernetes) and secrets; telemetry and version checks are off |
| zizmor | GitHub Actions workflows: template injection, untrusted triggers, unpinned actions, broad permissions (runs offline, medium severity and up) |

`-v` lists up to 60 findings per scanner instead of 8.

### `dev docs check [<paths>…] [--online] [--fix] [--no-prose]`

Checks documentation in one pass, covering Markdown structure (markdownlint), links and anchors (lychee), prose (Vale with the
Google and write-good styles) and, when no paths are given, every SVG in the repository. The repository's own
configuration files win over devkit's defaults.

| Option | Effect |
| --- | --- |
| `<paths>` | Files or folders to check (default: the whole repository) |
| `--online` | Also check external links |
| `--fix` | Apply markdownlint's automatic fixes first |
| `--no-prose` | Skip Vale |

### `dev diagram render | check | new`

Branded, validated SVG diagrams from JSON specs. See [Diagrams](diagrams.md).

| Command | Effect |
| --- | --- |
| `dev diagram new <kind> <name> [--dir DIR]` | Scaffolds a spec: `flow`, `c4`, `state`, `sequence`, `er`, or `timeline` |
| `dev diagram render <spec \| dir> [--out DIR] [--png]` | Writes `<name>-dark.svg` and `<name>-light.svg` (specs in `docs/diagrams/` go to `docs/images/diagrams/`); `--png` adds 2x previews |
| `dev diagram check <spec \| svg \| dir>` | Validates specs or SVG files; exits with `1` on problems |

<!-- TODO(devkit-docs): files-and-git -->

## Tracking and disk

### `dev canary [--days N] [--limit N]`

The instruction-canary report: per agent, how many tracked replies in the last `N` days (default 7) started with the
`[Firefly> DevKit]` marker, then the most recent replies without it (default 20) with an excerpt. Replies that carry
the marker are also scored for the format of the status block; `dev`, `dev overview`, and `dev doctor` show that score
as `format N%`.

### `dev sync [--offline] [--scheduled] [--no-push] [--quiet]`

Rebuilds the projects map (`PROJECTS.md`, `TASKS.md`, `ACTIVITY.md`, `registry.json`), the generated blocks of the
handoff notes, this machine's presence file and the dashboard, then commits and pushes the handoff repository when
something changed.

| Option | Effect |
| --- | --- |
| `--offline` | No network; cached PR data of any age |
| `--scheduled` | Background mode: pull first, release expired locks, refresh PR states and sizes, apply the safe cleanup when `gc.auto` is on, rotate logs |
| `--no-push` | Commit but do not push |
| `--quiet` | No output |

### `dev gc [--apply] [-y] [-v]`

Plans disk reclamation with fresh PR states and prints what can be reclaimed (a dry run). `-v` also lists what is kept
and why. `--apply` asks for confirmation in a terminal (skip it with `-y`); in a non-interactive shell it refuses
unless you pass `--yes`. It then syncs. The rules are in [Disk and cleanup](disk-and-cleanup.md).

### `dev du [--refresh]`

Sizes of every project's main checkout and worktrees, Claude Code transcripts and temp, the Codex home, devkit temp,
the npm and Homebrew caches, the Colima VM disk, and `docker system df` when Docker is running. `--refresh` recomputes
cached sizes.

## Environment

### `dev setup [-y] [--skip-packages]`

The installation and configuration wizard. It detects the Mac, GitHub, and both agents, asks for the development root,
the handoff repository, which agents to configure and their permission modes, your stacks, the Docker VM, automation
and the settings lock, shows the plan, and applies it only after you confirm: packages for the chosen stacks, then
`dev install`, the sign-ins, and `dev doctor`. Run it again to change your choices.

| Option | Effect |
| --- | --- |
| `-y`, `--yes` | Accept every default, without questions |
| `--skip-packages` | Do not install Homebrew or npm packages |

### `dev doctor`

Checks the toolchain, GitHub sign-in and git identity, the workspace (PATH, shell, config, handoff repository with its
push state, layout), the generated instructions and skills, Claude Code (permission mode, attribution, hooks,
plugins, MCP servers), Codex (approval and sandbox, hook trust, profile models, MCP servers, plugins), both agents'
sign-in and quota, the devkit plugin (validators, and whether each agent's installed copy is current), project
settings that switch devkit off, sessions that ran with devkit in the last 7 days, Docker, the background job, the
instruction canary (fails below 90% over 7 days) and free disk. Each failure prints its fix. Exits with `1` if any
check fails.

### `dev agents [status | test] [claude | codex | all] [--json] [--model MODEL] [-v]`

`dev agents` reports, for Claude Code and Codex: version, desktop app, sign-in method, plan, account, login expiry,
quota and the last self-test. `--json` prints the same as JSON.

`dev agents test [claude|codex]` runs a short real session in a scratch repository and checks the integration from
recorded evidence: the session ran, the SessionStart hook recorded it, the guard blocked a tool-prefixed branch, the
reply carried the status block (and its format score), the global instructions were loaded, and the devkit plugin
skills are visible. `--model` picks the model for that session, and `-v` prints the agent's reply.

### `dev config [diff | repair | show | edit | validate | lock | unlock | status] [--quiet]`

Previews and enforces the agent settings that devkit manages. See [Agents](agents.md).

| Command | Effect |
| --- | --- |
| `dev config` · `dev config diff` | Every difference between devkit's configuration and what is installed (Claude Code, Codex, instructions, skills, git). Exits with `1` when there are differences |
| `dev config repair [--quiet]` | Restores the managed keys and logs what changed. Session start runs it in the background |
| `dev config show` | The location of the config file and of devkit, and the effective configuration as JSON |
| `dev config edit` | Opens `~/.config/devkit/config.json` in your editor, then validates it |
| `dev config validate` | Checks the config file and devkit's own configuration files |
| `dev config lock` · `unlock` | Installs or removes a Claude Code managed-settings file (asks for your administrator password) that user settings cannot override |
| `dev config status` | The lock state and the most recent configuration events |

### `dev guard check "<command>" | --write PATH [--agent claude|codex] [--cwd DIR]`

Shows what the guard decides for a shell command or a file write, without running anything. Quote a command that has
its own options. Exits with `1` when the guard would deny or ask.

```bash
dev guard check "git push --force"
dev guard check --write ~/Desktop/notes.md --agent codex
```

### `dev plugin validate`

Validates the devkit plugin with Claude Code's validator and with Codex's own manifest parser (version, display name,
logo and skills).

### `dev install [--only STEPS]`

Applies the configuration; safe to run again. `STEPS` is a comma-separated list of
`config,cli,git,handoff,instructions,skills,claude,codex,docker,docs,launchd`. See
[Getting started › What dev install configures](getting-started.md#what-dev-install-configures).

### `dev update`

Runs `git pull --ff-only` in the devkit repository, then `dev install`.

### `dev docker up | down | status | prune | autostart-on | autostart-off`

| Action | Effect |
| --- | --- |
| `up` | Start Colima with the configured resources (created on the first run) and print the Docker server summary |
| `down` | `colima stop` |
| `status` | `colima status` and `docker system df` |
| `prune` | `docker system prune -f` and `docker builder prune -f` (never volumes) |
| `autostart-on` · `autostart-off` | Start Colima at login with `brew services` (or stop doing so) |

### `dev version`

Version, install path, and copyright.

## Shell integration

`dev install` makes `~/.zshrc` source `shell/devkit.zsh`, which adds:

| Function | Effect |
| --- | --- |
| `dcd <query>` | `cd` into a project (fzf picker when more than one matches) |
| `dwt <branch> [--base REF]` | Create or reuse a managed worktree and `cd` into it |

It also puts `~/.local/bin` on the PATH, sets the Docker and telemetry environment variables, and loads zsh completion
for `dev` (commands, project ids, worktree branches, Docker actions, install steps) and `dcd`.

## Internal commands

`dev hook <event> --agent claude|codex` is the hook entry point that Claude Code and Codex call (see
[Agents › Hooks](agents.md#hooks)). `dev __complete <projects|branches|commands>` feeds shell completion.

See also [Concepts](concepts.md), [Configuration](configuration.md), and [Teams and machines](teams.md).
