# devkit documentation

devkit gives you, Claude Code, and Codex one predictable workspace on macOS: every repository in a known place, shared
rules and skills for both agents, a guard that stops the habits that waste time and disk, local-first delivery, and a
handoff repository that keeps the state of every initiative, project, and task current for every developer and
machine.

## Start here

| Guide | What it covers |
| --- | --- |
| [Getting started](getting-started.md) | Installing on a new Mac, the setup wizard, onboarding another developer, the first-day checklist, updating, what devkit touches |
| [Concepts](concepts.md) | Workspace layout, project ids, how work is organized, from plan to operate, the handoff repository and notes, worktrees, the guard, the status block, and background sync |

## Work with devkit

| Guide | What it covers |
| --- | --- |
| [Teams and machines](teams.md) | Sharing a handoff repository: work items, locks, reasons, and history, delegation, versioned plans, session logs, presence, merge rules |
| [Initiatives](initiatives.md) | Cross-project initiatives: creating them, who may write what, progress, adopting an older handoff repository |
| [Progress and communication](progress-and-communication.md) | `dev progress`, ETA and calibrated estimates, notifications, what the hooks add, `dev time`, the message templates, status reports |
| [Dashboard](dashboard.md) | The HTML command center: views, initiative and project pages, the task drawer, search, shortcuts, routes |
| [Delivery](delivery.md) | The delivery policy, local-first GitOps (one integration branch, one push, one CI run), following CI with `dev ci status`, releases |
| [Diagrams](diagrams.md) | `dev diagram`: specs, validation, design rules, devkit's own diagrams |
| [Disk and cleanup](disk-and-cleanup.md) | `dev gc` safety rules, automatic cleanup, `dev du`, agent data retention |
| [Docker](docker.md) | Colima, Compose, and Buildx, Testcontainers, amd64 images |

<!-- TODO(devkit-docs): files-and-git — add a guide for the git and GitHub wrapper (dev branch, commit, push, pr, run, release, issue) once it is merged. -->

## Reference

| Guide | What it covers |
| --- | --- |
| [Commands](commands.md) | Every `dev` command and option, the file commands, `dev ci status`, `dev delivery`, the `dcd` and `dwt` shell functions, and completion |
| [Configuration](configuration.md) | Every key of `~/.config/devkit/config.json`, personal agent rules, environment variables, the files that apply to everyone |
| [Agents](agents.md) | What devkit configures in Claude Code and Codex, the global instructions, every hook, how devkit stays on |
| [Skills and plugins](skills-and-plugins.md) | The devkit plugin, required skills, the curated catalog and optional groups, and what was left out |
| [Models and tokens](models-and-tokens.md) | Models, effort, and subagents; the scout agent; Codex profiles; the routing rules |
| [Templates](templates.md) | Plan, spec, spike, bug-fix, and ADR templates; task templates; the pull request template |
| [Guard rules](guard.md) | Every rule the guard enforces, per agent, with example messages, and escape hatches |
| [Troubleshooting](troubleshooting.md) | Logs, common problems, and how to reset |

## Maintain devkit

| Guide | What it covers |
| --- | --- |
| [Developing devkit](development.md) | Code layout, design principles, tests, the release process |

## Quick reference

```bash
dev                 # your workspace at a glance
dev help            # all commands
dev doctor          # verify the setup
dev <command> -h    # options of one command
```

See also [the project README](../README.md) and [the CHANGELOG](../CHANGELOG.md).
