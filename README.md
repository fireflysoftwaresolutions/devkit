<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/hero-dark.webp">
  <img src="docs/images/hero-light.webp" alt="devkit — one workspace for developers, Claude Code and Codex. By Firefly Software Solutions Inc." width="100%">
</picture>

# devkit

devkit is how Firefly Software developers work with Claude Code and Codex: every repository in a predictable place,
every project with a living handoff note and task board, every agent session starting from the current state and
ending by recording what changed, and a workspace that never fills up with stray worktrees, sibling copies or
gigabytes of forgotten build output. One install sets up the toolchain, git, Claude Code (CLI and Desktop), Codex (CLI
and the ChatGPT app), skills, plugins, Docker and the background automation, and keeps them that way.

[Why](#why-devkit) ·
[Install](#install) ·
[Everyday use](#everyday-use) ·
[For your agents](#for-your-agents) ·
[How it fits together](#how-it-fits-together) ·
[What's inside](#whats-inside) ·
[Docs](#documentation) ·
[Credits](#credits-and-licence)

## Why devkit

| Without devkit | With devkit |
|---|---|
| Each session rediscovers the project | The session starts with the project's handoff note, tasks, git state, commands and the last agent status |
| Worktrees and `repo-pr12` copies pile up; disks fill | One managed worktree location, a guard that blocks stray ones, and safe automatic cleanup once work is merged |
| Push → CI → fix → push → CI … | `dev ci` shows what CI runs; integrate locally, push once, one CI run; the guard stops CI loops and blind re-runs |
| `claude/vigilant-jang-ec71c0` branches and AI attribution | Trunk-based naming (`feat/PROJ-142-invoice-export`) and no attribution, enforced for both agents |
| Claude and Codex behave differently, and settings drift | One set of rules, skills and hooks for both; drift is blocked and repaired; `dev config diff` previews everything |
| Nobody knows what is in flight | A private handoff repo with a live projects map, task boards, activity, and a dashboard |
| "Did the agent even read the rules?" | Every reply starts with a `DevKit ·` status line; devkit checks it, records it and reports deviations |

## Install

```bash
gh auth login -s repo,read:org,workflow,gist                      # once
gh repo clone fireflysoftwaresolutions/devkit ~/Development/fireflysoftwaresolutions/devkit
~/Development/fireflysoftwaresolutions/devkit/install.sh          # everything, idempotent
exec zsh && dev doctor                                             # all green
```

`install.sh` installs the toolchain (Java, Python, PHP, Node/TypeScript, language servers, Docker on Colima), signs git
in with your GitHub identity, creates your private handoff repository (`<you>/handoff`), configures Claude Code and
Codex, installs the devkit plugin and the curated skills in both, and schedules the hourly sync. Details:
[Getting started](docs/getting-started.md).

## Everyday use

```text
dev                          your workspace at a glance
dev dashboard                the command centre in the browser: projects, tasks, activity, explorer, health
dev overview                 every project: status, Now tasks, latest agent status, last session
dcd kit                      jump into a project            dwt feat/invoice-export   …or a new worktree
dev task add "Export invoices as CSV" --now      dev task done 1      dev note set status=blocked
dev claude api -w feat/x     Claude Code in a managed worktree       dev codex api   Codex in the project
dev ci                       what CI runs — run it locally, then push once
dev standup                  what happened since yesterday
dev gc                       what can be reclaimed safely (dry run)
dev config diff              what devkit manages vs what is installed      dev guard check git push --force
dev doctor                   verify the whole setup
```

All commands: [docs/commands.md](docs/commands.md).

## For your agents

devkit is also a plugin for **Claude Code** and **Codex**, installed automatically:

- **Context in, record out.** Each session receives the project's handoff note, tasks, branch, PRs, commands and the
  last status line; it ends with its commits and status recorded in the note, and the handoff repo pushed.
- **Slash commands:** `/devkit:status` · `/devkit:overview` · `/devkit:task` · `/devkit:handoff` · `/devkit:standup` ·
  `/devkit:ship` · `/devkit:ci` · `/devkit:dashboard` · `/devkit:doctor` (skills of the same name in Codex).
- **Skills:** `handoff`, `workspace`, `delivery` (local-first GitOps), `backend` (contracts, migrations, testing,
  resilience), plus a curated set for architecture (ADRs, C4, domain modelling), Angular, React, Spring and testing.
- **Rules:** one set of global instructions for both agents: method, files and disk, worktrees, branches, commits,
  delivery, backend practice, handoff, and the `DevKit ·` status line.
- **Guard:** a pre-tool hook that blocks what wastes time or disk, and explains the right way in one sentence.

## How it fits together

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/diagrams/architecture-dark.svg">
  <img src="docs/images/diagrams/architecture-light.svg" alt="How devkit fits together: the devkit repo installs rules, the devkit plugin and hooks into Claude Code (CLI and Desktop) and Codex (CLI and the ChatGPT app); the agents work in your repositories and exchange context and session records with the private handoff repo, which an hourly sync keeps current; repositories and the handoff repo are pushed to GitHub, and the dashboard and overview read the handoff repo." width="100%">
</picture>

Concepts: [docs/concepts.md](docs/concepts.md).

## What's inside

| Path | What |
|---|---|
| `bin/dev`, `lib/devkit/` | The `dev` CLI (Python 3.9+, standard library only) |
| `plugin/` | The devkit plugin for Claude Code and Codex: skills, slash commands, identity |
| `agents/AGENTS.md` | The global instructions generated into `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` |
| `config/` | What devkit manages: Claude and Codex settings, hooks, plugins, MCP servers, pinned skills, git defaults |
| `install.sh`, `Brewfile` | Bootstrap and toolchain |
| `shell/` | zsh integration (`dcd`, `dwt`, completion, Docker environment) |
| `templates/handoff/` | The skeleton of a new handoff repository |
| `tools/brand/` | Renders devkit's icon and banners from the Firefly brand elements |
| `docs/` | The guides below |
| `tests/` | Unit tests (`python3 -m unittest discover -s tests`) |

## Documentation

| Guide | For |
|---|---|
| [Getting started](docs/getting-started.md) | Install on a new Mac, onboard a developer, update, uninstall |
| [Concepts](docs/concepts.md) | Layout, handoff notes and tasks, worktrees, guard, canary, sync |
| [Commands](docs/commands.md) | Every `dev` command and option |
| [Dashboard](docs/dashboard.md) | The HTML command centre: views, filters, explorer, shortcuts |
| [Configuration](docs/configuration.md) | `~/.config/devkit/config.json`, personal rules, environment variables |
| [Agents](docs/agents.md) | What is configured in Claude Code and Codex; hooks; enforcement and lock |
| [Skills and plugins](docs/skills-and-plugins.md) | The devkit plugin, the curated catalog and what was left out |
| [Delivery](docs/delivery.md) | Local-first GitOps: branch model, one integration, one push, one CI run |
| [Guard rules](docs/guard.md) | Everything the guard blocks, and why |
| [Disk and cleanup](docs/disk-and-cleanup.md) | `dev gc` safety rules and automatic cleanup |
| [Docker](docs/docker.md) | Colima, Compose, Buildx, Testcontainers |
| [Troubleshooting](docs/troubleshooting.md) | Logs, common problems, resetting |
| [Developing devkit](docs/development.md) | Code layout, tests, brand assets, release process |

## Credits and licence

devkit is designed and maintained by **Andrés Contreras Guillén** for **Firefly Software Solutions Inc.**

Copyright © 2026 Firefly Software Solutions Inc. All rights reserved. Proprietary — see [LICENSE](LICENSE).
Third-party skills and tools installed by devkit keep their own licences — see [NOTICE](NOTICE).
