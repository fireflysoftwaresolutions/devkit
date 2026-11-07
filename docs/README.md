# devkit documentation

devkit gives you, Claude Code, and Codex one predictable workspace on macOS: every repository in a known place, shared
rules and skills for both agents, a guard that stops the habits that waste time and disk, local-first delivery, and a
handoff repository that keeps the state of every initiative, project, and task current for every developer and
machine.

## Start here

| Guide | What it covers |
| --- | --- |
| [Getting started](getting-started.md) | Your first install step by step, even without a terminal habit; the setup wizard, the pinned copy, onboarding another developer, the first-day checklist, updating, what devkit touches |
| [Where to use devkit](surfaces.md) | The six surfaces compared: hooks, guard, crew, routing, skills in briefs, and status block checks on each, and which tutorial to follow |
| [Concepts](concepts.md) | Workspace layout, project ids, how work is organized, from plan to operate, the handoff repository and notes, worktrees, the guard, the status block, and background sync and scheduled jobs |

## Tutorials: your first task

Each tutorial opens a project, gives the crew a first task, and explains everything you see on the way: the status
block, progress updates, approvals, the crew graph, and the dashboard.

| Tutorial | Where you work |
| --- | --- |
| [Claude Code](tutorials/claude-code.md) | Claude Code in Terminal, VS Code, or a JetBrains IDE |
| [Claude Desktop's Code tab](tutorials/claude-desktop.md) | The Code tab of Claude Desktop, where development happens in the app |
| [Codex CLI](tutorials/codex-cli.md) | Codex in Terminal (not verified live yet) |
| [Codex in the ChatGPT app](tutorials/chatgpt-codex.md) | Codex in the ChatGPT desktop app (not verified live yet) |
| [Claude Desktop chat and Cowork](tutorials/desktop-chat-and-cowork.md) | Status, plans, and reports through the devkit extension; code changes go to the Code tab |

## Work with devkit

| Guide | What it covers |
| --- | --- |
| [Teams and machines](teams.md) | Sharing a handoff repository: work items, locks, reasons, and history, delegation, versioned plans, session logs, presence, merge rules |
| [Initiatives](initiatives.md) | Cross-project initiatives: creating them, who may write what, progress, adopting an older handoff repository |
| [The crew](crew.md) | devkit's 25 specialist agents and the tech lead (Morpheus): who does what, parallel or in sequence, slots, lanes, scopes, messages, delegation, guided or automatic decisions, the crew canary |
| [Progress and communication](progress-and-communication.md) | `dev progress`, ETA and calibrated estimates, notifications, what the hooks add, `dev time`, the message templates, status reports |
| [Continuity](continuity.md) | What devkit keeps of a session through compaction, resume, and `/clear`, the continuity brief, `dev session`, and `devkit_resume` in chat |
| [Dashboard](dashboard.md) | The live command center: `dev dashboard` and its server, the views, Background work, the crew, drawers, live and honest states, search, shortcuts, routes |
| [Git and GitHub](git-and-github.md) | `dev branch`, `commit`, `rebase`, `push`, `pr`, `run`, `release`, and `issue`: local CI evidence, the PR template, safe merges, releases by the policy |
| [Testing](testing.md) | `dev test`: failed first, then the blast radius of your changes; `dev test audit` and `dev test prove`: tests that can fail; `dev ci --run --resume` |
| [CI/CD pipelines](ci-cd.md) | `dev ci audit` and `dev ci init` on GitHub Actions, GitLab, Bitbucket, and Azure; `dev ci watch`: every run followed to the end, failures diagnosed |
| [Delivery](delivery.md) | The delivery policy, local-first GitOps (one integration branch, one push, one CI run), following CI with `dev ci status`, releases |
| [Lessons](lessons.md) | Learning from mistakes: recording, recalling, counting repeats, automating prevention, and `dev sleep` |
| [Scheduled jobs and the audit trail](jobs-and-audit.md) | `dev jobs`: the background jobs, their schedules, logs, and journal; `dev audit`: everything devkit changed or decided |
| [The audit trail](audit.md) | What each record holds (schema v2), levels, the hash chain and `dev audit verify`, queries and exports (JSONL, CSV, OCSF, CEF), retention, and the GDPR rights of the people recorded |
| [Diagrams](diagrams.md) | `dev diagram`: specs, validation, design rules, devkit's own diagrams |
| [Disk and cleanup](disk-and-cleanup.md) | `dev gc` safety rules, automatic cleanup, `dev du`, agent data retention |
| [Docker](docker.md) | Colima, Compose, and Buildx, Testcontainers, amd64 images |

## Reference

| Guide | What it covers |
| --- | --- |
| [Commands](commands.md) | Every `dev` command and option: git and GitHub, `dev progress` and `dev crew`, lessons, jobs and audit, the file commands, `dev test` and `dev stubs`, `dev ci status`, `watch`, `audit`, and `init`, `dev delivery`, the `dcd` and `dwt` shell functions, and completion |
| [Configuration](configuration.md) | Every key of `~/.config/devkit/config.json`, personal agent rules, environment variables, the files that apply to everyone |
| [Agents](agents.md) | What devkit configures in Claude Code and Codex, the crew's agents and Codex roles, the global instructions, every hook, how devkit stays on |
| [Skills and plugins](skills-and-plugins.md) | The devkit plugin, required skills, the curated catalog and optional groups, and what was left out |
| [Models and tokens](models-and-tokens.md) | Models, effort, and subagents; the Lookup scout (Sparks); Codex profiles; the routing rules |
| [Templates](templates.md) | Plan, spec, spike, bug-fix, and ADR templates; task templates; the pull request template |
| [Guard rules](guard.md) | Every rule the guard enforces, per agent, including devkit's own state and the forged-environment rules, with example messages, and escape hatches |
| [Troubleshooting](troubleshooting.md) | Logs, common problems (pin refusals and rollback included), and how to reset |

## Maintain devkit

| Guide | What it covers |
| --- | --- |
| [Developing devkit](development.md) | Code layout, design principles, tests, trying a change through the pinned copy, the release process |

## Quick reference

```bash
dev                 # your workspace at a glance
dev help            # all commands
dev doctor          # verify the setup
dev <command> -h    # options of one command
```

See also [the project README](../README.md) and [the CHANGELOG](../CHANGELOG.md).
