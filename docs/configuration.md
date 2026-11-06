# Configuration

## `~/.config/devkit/config.json`

Created by `dev install` on first run. Every key is optional; missing keys take the defaults below. Nested objects
are merged with the defaults, so you only write what you change.

```json
{
  "dev_root": "~/Development",
  "github_user": "ancongui",
  "handoff_repo": "ancongui/handoff",
  "gc": { "auto": true },
  "sync": { "push": true }
}
```

| Key | Default | Meaning |
|---|---|---|
| `dev_root` | `~/Development` | Root of the workspace |
| `github_user` | detected with `gh api user` | Your GitHub login |
| `handoff_repo` | `<github_user>/handoff` | The private repository that holds the map and handoff notes |
| `handoff_dir` | `<dev_root>/.handoff` | Local clone of the handoff repository |
| `default_host` | `github.com` | Host whose repositories live at `<owner>/<repo>`; all others live at `<host>/<group…>/<repo>` |
| `host_dirs` | `{}` | Folder aliases for hosts, for example `{"gitlab.com": "gitlab"}` → `~/Development/gitlab/<group>/<repo>` |
| `sync.push` | `true` | Push the handoff repository after each sync |
| `sync.interval_minutes` | `60` | Interval of the launchd background job (re-run `dev install --only launchd` after changing it) |
| `gc.auto` | `true` | The background job applies the safe cleanup automatically |
| `gc.grace_hours` | `24` | Idle time before a merged or fully contained worktree is removed |
| `gc.idle_artefact_days` | `7` | Idle time before regenerable build output is pruned in a worktree |
| `gc.temp_days` | `7` | Age after which temp entries are purged |
| `gc.docker` | `true` | Also prune dangling Docker images and build cache older than 7 days during cleanup |
| `docker.cpu` | `6` | Colima VM CPUs |
| `docker.memory` | `12` | Colima VM memory in GB |
| `docker.disk` | `80` | Colima VM disk in GB (sparse; can only grow) |
| `docker.vm_type` | `vz` | Apple Virtualization framework |
| `docker.rosetta` | `true` | Rosetta for amd64 images (with `vz`) |
| `docker.mount_type` | `virtiofs` | Host folder sharing |
| `docker.autostart` | `false` | When `true`, `dev install` registers Colima with `brew services` so it starts at login (same as `dev docker autostart-on`) |
| `editor` | `code` | Editor used by `dev open` (and by `dev handoff --edit` when `$EDITOR` is unset) |

Changing `dev_root`, `default_host` or `host_dirs` changes every canonical path: run `dev ls` and `dev adopt` the
repositories that are no longer in place.

### For another developer

Nothing needs editing: `dev install` detects their login and uses `<their-login>/handoff`. To share a team handoff
repository or use another name, set `handoff_repo` before running `dev install`.

## Personal agent rules

`~/.config/devkit/AGENTS.personal.md` is appended, under "Personal additions", to the generated
`~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`. Use it for preferences that are yours only (language of replies,
editor habits). Shared rules belong in `agents/AGENTS.md` in the devkit repository.

```bash
$EDITOR ~/.config/devkit/AGENTS.personal.md
dev install --only instructions
```

Never edit the generated files directly; the next install overwrites them.

## Environment variables

| Variable | Effect |
|---|---|
| `DEV_ROOT` | Overrides `dev_root` |
| `DEVKIT_CONFIG` | Path of the config file (default `~/.config/devkit/config.json`) |
| `DEVKIT_GUARD=off` | Set in the environment of the agent process (`DEVKIT_GUARD=off claude`), disables the guard for that whole session (see [Guard rules](guard.md#escape-hatch)) |
| `DEVKIT_LOCAL_CI_PASSED=1` | Prefix of a `git push`: acknowledges that the full local suite just passed, lifting the push budget for that push |
| `DEVKIT_FLAKY=1` | Prefix of `gh run rerun`: acknowledges a proven flaky failure |
| `CODEX_HOME` | Codex home (default `~/.codex`) |
| `CLAUDE_CODE_TMPDIR` | Claude Code's temp root; devkit treats it as temp and purges it from there |

The shell integration and the agents' environments also set `DOCKER_HOST`, `TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE`,
`NG_CLI_ANALYTICS=false`, `NEXT_TELEMETRY_DISABLED=1` and `SUPERPOWERS_DISABLE_TELEMETRY=1`.

## Plugins, MCP servers and skills

The lists live in the devkit repository and apply to everyone who installs it:

| File | Controls |
|---|---|
| `config/claude/plugins.json` | Claude Code marketplaces, plugins (`plugins`, `disabled`) and user-scope MCP servers |
| `config/claude/settings.json` | The settings fragment merged into `~/.claude/settings.json`, including hooks and `env` |
| `config/codex/plugins.json` | Codex plugins and MCP servers |
| `config/codex/config.json` | Codex settings written through Codex's config API (`settings` replaced, `upsert` merged) |
| `config/codex/hooks.json` | Codex hooks |
| `config/skills.json` | Third-party skills: repository, pinned commit (`ref`), licence, and skill folders |
| `agents/skills/` | devkit's own skills |

Placeholders `{{DEVKIT}}`, `{{HOME}}`, `{{DEV}}` and `{{UID}}` are expanded at install time. After editing:

```bash
dev install --only skills,claude,codex   # or the step you changed
dev doctor
```

To bump a third-party skill, review the upstream change, put the new full commit SHA in `ref` and re-run
`dev install --only skills`. Fetched sources are cached per commit in `~/.local/share/devkit/skills-src/`.

See also: [Agents](agents.md) · [Skills and plugins](skills-and-plugins.md)
