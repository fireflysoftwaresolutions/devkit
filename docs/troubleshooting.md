# Troubleshooting

This page lists where devkit keeps its logs and state, the problems people run into most, and how to reset. Start
with `dev doctor`: every failed check prints the command that fixes it.

## Logs and state

| Path | Content |
| --- | --- |
| `~/.local/state/devkit/logs/hooks.log` | Hook errors (with tracebacks), every guard deny and ask, skill reminders, sessions registered at their first prompt |
| `~/.local/state/devkit/logs/sync.log` | Background syncs (launchd and session hooks); rotated at 2 MB |
| `~/.local/state/devkit/logs/config.log` | Settings restored by `dev config repair` and changes refused by ConfigChange |
| `~/.local/state/devkit/canary.jsonl` | Status block results per reply: marker, format score, excerpt of misses (90 days) |
| `~/.local/state/devkit/status/` | The latest status block per project, stored as one line |
| `~/.local/state/devkit/sessions/` | Session records (expire after 7 days) |
| `~/.local/state/devkit/runs/` | `dev progress` runs, one file per session |
| `~/.local/state/devkit/timing/` | Turns, slow commands, subagents, and finished runs, one file per month (`dev time`) |
| `~/.local/state/devkit/backups/` | Copies of every file devkit rewrote |
| `~/.local/state/devkit/cache/` | GitHub user and organizations, sizes; `prs/` holds PR lists |

```bash
launchctl print gui/$UID/com.devkit.sync | head -30     # is the background job loaded? last exit code
tail -50 ~/.local/state/devkit/logs/sync.log
dev sync                                                  # run a sync in the foreground
```

## Sessions and agents

### Claude Code or Codex does not show the devkit context

Restart the session: instructions and hooks are read at session start. A session that missed SessionStart gets the
context at its first prompt, and `hooks.log` records it. If neither happens, check that `~/.claude/settings.json`
contains devkit's hooks (`dev doctor`, Claude section), that Codex trusts them (next problem), and look for errors in
`hooks.log`. `dev doctor` also reports project settings that switch devkit off.

### Codex hooks are not running

`dev doctor` shows `devkit hooks trusted` failing. Codex skips user hooks until they are trusted, and trust is tied to
the hook's definition.

```bash
dev install --only codex      # trusts devkit's hooks again through Codex's API
```

Or review them in Codex with `/hooks` (CLI) or Settings → Hooks (app). Restart open Codex sessions.

### A devkit skill or the scout agent is missing

Both agents install a snapshot of the devkit plugin. `dev doctor` reports "devkit plugin copy is current" for each;
when it fails, run `dev install --only claude` or `dev install --only codex`, which reinstalls the plugin from your
checkout, then restart the agent.

### The status block is missing or scored as malformed

`dev canary` lists replies that did not start with the `[Firefly> DevKit]` marker; `dev` and `dev overview` show the
format score.

- Many misses from one agent: check that its instruction file exists and is generated (`~/.claude/CLAUDE.md` or
  `~/.codex/AGENTS.md`), then run `dev install --only instructions` and restart its sessions.
- A low format score: the agent writes an older one-line form, or leaves out a bullet. Sessions that loaded
  instructions from before 26.11.0 do this until they restart.
- A repository with a long AGENTS.md can crowd out the global instructions in Codex; keep repository files
  focused.

Only sessions that started after devkit was installed are tracked.

### The agent keeps being sent back at the end of a turn

The Stop hook asks for one more pass, never in a loop, when:

| Reason in the message | Fix |
| --- | --- |
| The final message lacks the status block | The agent replies again with it. `canary.enforce: false` turns this off |
| A plan changed without a recorded reason | `dev plan revise <file> --why "…"` for each plan it names |
| Files changed without a required skill | The agent loads the skill and reviews the change. `skills.enforce: false` turns this off |
| Commits without a handoff update | The agent updates the note with the `handoff` skill. `handoff.enforce: false` turns this off |

### Codex profile models are not available

`dev doctor` reports "profile models available" failing when your account does not offer a profile's model. List the
models with `codex debug models`, set `codex.profiles` in `~/.config/devkit/config.json` (see
[Models and tokens](models-and-tokens.md#configuration-keys)), and run `dev install --only codex`.

### No desktop notifications

Check `progress.notify` in your configuration and that `DEVKIT_NO_NOTIFY` is not set. macOS shows them as coming from
Script Editor; allow its notifications in System Settings → Notifications.

## Tasks and the handoff repository

### A task is claimed by someone who stopped working

The session context and `dev team` show who holds it and until when. Ask them first. A lock that is not renewed
expires after `work.lock_hours` (8) and the hourly sync on any machine releases it; after that anyone can claim it. A
lock that has not expired cannot be released by someone else.

### `dev task start` says the task was claimed on another machine first

Two machines claimed the same item within seconds; the first push won. Pick another task, or talk to the holder.

### A change is refused with "give the reason with --why"

`edit`, `move`, and `cancel` change the scope or the plan, so devkit records why. Add `--why "…"`.

### The handoff repository does not push

```bash
cd ~/Development/.handoff && git status && git log --oneline -3 && git push
gh auth status
```

Check that `handoff_repo` in `~/.config/devkit/config.json` matches the `origin` remote, and that `gh auth setup-git`
has run (`dev install --only git`). If `git status` shows a merge conflict (two people changed the same work item at
the same moment), resolve it in the handoff repository, commit, and run `dev sync`. If `.gitattributes` lacks the
devkit merge rules, run `dev install --only handoff`.

## Configuration

### `dev config validate` reports `ui`, `progress`, `work`, or `language` as unknown keys

The validator's list of keys is older than these keys. `ui`, `progress`, and `work` take effect anyway; `language` is
reserved (see [Configuration](configuration.md#workspace)).

### `dev guard check` says "unrecognized arguments"

Quote a command that has its own options: `dev guard check "git switch -c fix/login"`.

## Workspace

### The guard blocked something legitimate

Read the reason in the agent's output or in `hooks.log`. Most denials describe the intended way (`dev wt new`,
`dev clone`, `--force-with-lease`, `dev task …`, `DEVKIT_LOCAL_CI_PASSED=1`, `DEVKIT_FLAKY=1`). For a genuine false
positive, run that session with the guard off and report the case, so the rule can be fixed with a test:

```bash
DEVKIT_GUARD=off claude       # or codex; applies to the whole session
```

### `dev adopt` refuses to move a repository

It refuses while any process works inside the repository or its worktrees, and lists them. Close those Claude Code,
Claude Desktop, Codex, terminal, and editor sessions, then run it again.

### Claude Desktop or Codex app conversations after `dev adopt`

Conversations opened at the old path do not resume there. Start new ones from the new path. In the terminal,
`claude --resume` works from the new path because devkit moved the history folder.

### An LSP plugin does nothing

The Claude Code LSP plugins need their language servers: `typescript-language-server` and `typescript`, `pyright`,
`jdtls` (which needs a Java 25 runtime) and `intelephense`. `dev doctor` checks that they are installed, and
`dev setup` installs them for your stacks.

TypeScript 7 (the native port) ships no `tsserver.js`; `typescript-language-server` then falls back to another
TypeScript install, so diagnostics may not match a TypeScript 7 project.

### Docker commands fail

```bash
dev docker status          # is the VM running?
dev docker up
docker context ls          # colima should be current
echo $DOCKER_HOST          # unix://…/.colima/default/docker.sock (open a new terminal after install)
```

## Resetting

- Apply everything again: `dev install` (idempotent), or `dev setup` to change your choices.
- Restore one file: copy it back from `~/.local/state/devkit/backups/<name>.<timestamp>`.
- Fetch skills again: delete `~/.local/share/devkit/` and run `dev install --only skills`.
- Clear caches: delete `~/.local/state/devkit/cache/`.
- Stop the background job: `launchctl bootout gui/$UID/com.devkit.sync`.

See also [Getting started › What devkit touches](getting-started.md#what-devkit-touches-and-how-to-undo-it),
[Agents](agents.md), and [Teams and machines](teams.md).
