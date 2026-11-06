# devkit documentation

devkit gives you, Claude Code and Codex one predictable workspace on macOS: every repository in a known place,
shared rules and skills for both agents, a guard that stops the habits that waste time and disk, local-first
delivery, and a private handoff repository that keeps the state of every project up to date.

## Guides

| Guide | What it covers |
|---|---|
| [Getting started](getting-started.md) | Installing on a new Mac, onboarding another developer, the first-day checklist, updating, what devkit touches |
| [Concepts](concepts.md) | Workspace layout, project ids, the handoff repository and notes, worktree lifecycle, the guard, the instruction canary, background sync |
| [Commands](commands.md) | Every `dev` command and option, the `dcd` / `dwt` shell functions and completion |
| [Configuration](configuration.md) | `~/.config/devkit/config.json`, personal agent rules, environment variables, changing plugin and skill lists |
| [Agents](agents.md) | Exactly what devkit configures in Claude Code and Codex, the generated instruction files, and the hooks |
| [Skills and plugins](skills-and-plugins.md) | The curated catalog, why each item is there, what was left out, per-project vendor tooling |
| [Delivery](delivery.md) | Local-first GitOps: one integration branch, one push, one CI run |
| [Guard rules](guard.md) | Every rule the guard enforces, per agent, with example messages |
| [Disk and cleanup](disk-and-cleanup.md) | `dev gc` safety rules, automatic cleanup, `dev du`, agent data retention |
| [Docker](docker.md) | Colima, Compose and Buildx, Testcontainers, amd64 images |
| [Troubleshooting](troubleshooting.md) | Logs, common problems and how to reset |
| [Developing devkit](development.md) | Code layout, design principles, tests, the release process |

## Quick reference

```bash
dev                 # dashboard
dev help            # all commands
dev doctor          # verify the setup
dev <command> -h    # options of one command
```

See also: [the project README](../README.md).
