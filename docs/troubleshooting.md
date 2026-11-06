# Troubleshooting

Start with `dev doctor`: every failed check prints the command that fixes it.

## Logs and state

| Path | Content |
|---|---|
| `~/.local/state/devkit/logs/hooks.log` | Hook errors (with tracebacks) and every guard deny/ask |
| `~/.local/state/devkit/logs/sync.log` | Background syncs (launchd and session hooks); rotated at 2 MB |
| `~/.local/state/devkit/canary.jsonl` | Instruction-canary results (90 days) |
| `~/.local/state/devkit/sessions/` | Session start records (expire after 7 days) |
| `~/.local/state/devkit/backups/` | Copies of every file devkit rewrote |
| `~/.local/state/devkit/cache/` | GitHub user/orgs, sizes; `prs/` holds PR lists |

```bash
launchctl print gui/$UID/com.devkit.sync | head -30     # is the background job loaded? last exit code
tail -50 ~/.local/state/devkit/logs/sync.log
dev sync                                                  # run a sync in the foreground
```

## Common problems

### Codex hooks are not running

`dev doctor` shows `devkit hooks trusted` failing. Codex skips user hooks until they are trusted, and trust is
tied to the hook's definition.

```bash
dev install --only codex      # re-trusts devkit's hooks through Codex's API
```

Or review them in Codex with `/hooks` (CLI) or Settings → Hooks (app). Restart open Codex sessions.

### Claude Code or Codex does not show the devkit context

Restart the session: instructions and hooks are read at session start. Check that
`~/.claude/settings.json` contains devkit's hooks (`dev doctor`, Claude section) and look for errors in
`hooks.log`.

### Canary deviations

`dev canary` lists replies that did not start with `DevKit ·`.

- Many deviations from one agent: check that its instruction file exists and is generated
  (`~/.claude/CLAUDE.md` or `~/.codex/AGENTS.md`), then `dev install --only instructions` and restart its sessions.
- A repository with a very long AGENTS.md can crowd out global instructions in Codex; keep repository files focused.
- Occasional misses are real deviations: point the agent at its global instructions.

Only sessions that started after devkit was installed are tracked.

### The guard blocked something legitimate

Read the reason in the agent's output or in `hooks.log`. Most denials describe the intended way (`dev wt new`,
`dev clone`, `--force-with-lease`, `DEVKIT_LOCAL_CI_PASSED=1`, `DEVKIT_FLAKY=1`). For a genuine false positive, run
that session with the guard off and report the case so the rule can be fixed with a test:

```bash
DEVKIT_GUARD=off claude       # or codex; applies to the whole session
```

### The handoff repository does not push

```bash
cd ~/Development/.handoff && git status && git log --oneline -3 && git push
gh auth status
```

Check that `handoff_repo` in `~/.config/devkit/config.json` matches the `origin` remote, and that `gh auth setup-git`
has run (`dev install --only git`).

### `dev adopt` refuses to move a repository

It refuses while any process works inside the repository or its worktrees, and lists them. Close those Claude Code,
Claude Desktop, Codex, terminal and editor sessions, then run it again.

### Claude Desktop or Codex app conversations after `dev adopt`

Conversations opened at the old path will not resume there. Start new ones from the new path. In the terminal,
`claude --resume` works from the new path because devkit moved the history folder.

### An LSP plugin does nothing

The Claude Code LSP plugins need their language servers: `typescript-language-server` + `typescript`, `pyright`,
`jdtls` (needs a Java 25 runtime) and `intelephense`. `dev doctor` checks they are installed; `./install.sh` installs
them.

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

- Re-apply everything: `dev install` (idempotent).
- Restore one file: copy it back from `~/.local/state/devkit/backups/<name>.<timestamp>`.
- Re-fetch skills: delete `~/.local/share/devkit/` and run `dev install --only skills`.
- Clear caches: delete `~/.local/state/devkit/cache/`.
- Stop the background job: `launchctl bootout gui/$UID/com.devkit.sync`.

See also: [Getting started › What devkit touches](getting-started.md#what-devkit-touches-and-how-to-undo-it) ·
[Agents](agents.md)
