# devkit documentation

devkit gives you, Claude Code, and Codex one predictable workspace on macOS: every repository in a known place, shared
rules and skills for both agents, a guard that stops the habits that waste time and disk, local-first delivery, and a
handoff repository that keeps the state of every initiative, project, and task current for every developer and
machine.

## Start here

| Guide | What it covers |
| --- | --- |
| [Getting started](getting-started.md) | Your first install step by step, even without a terminal habit; the setup wizard, the pinned copy, Tab completion and the zsh helpers (`dev setup shell`), onboarding another developer, the first-day checklist, updating and `dev update --check`, what devkit touches |
| [Where to use devkit](surfaces.md) | The six surfaces compared: hooks, guard, crew, routing, skills in briefs, and status block checks on each, and which tutorial to follow |
| [Concepts](concepts.md) | Workspace layout, project ids, how work is organized, from plan to operate, the handoff repository and notes, worktrees, the guard, the status block, the session-start welcome, and background sync and scheduled jobs |

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
| [Teams and machines](teams.md) | Sharing a handoff repository: work items, locks, reasons, and history, shipped and stale items and `dev task reconcile`, delegation, versioned plans, session logs, presence, merge rules, a handoff repository that stopped syncing |
| [Work states](work-states.md) | The states of work items and locks, runs, steps, and crew lanes, decisions and crew messages, waits and CI watches, the pinned install, and branches after a merge: what moves each, where you see it, and how to repair drift |
| [Initiatives](initiatives.md) | Cross-project initiatives: creating them, who may write what, progress, adopting an older handoff repository |
| [The crew](crew.md) | devkit's 25 specialist agents and the tech lead (Morpheus): who does what, parallel or in sequence, slots, lanes, scopes, messages, delegation, guided or automatic decisions, moot and withdrawn decisions, how your answers to irreversible decisions count, the crew canary, and rooms (planned, not available yet) |
| [Progress and communication](progress-and-communication.md) | `dev progress`, when a run may end and `dev progress confirm`, ETA and calibrated estimates, notifications, what the hooks add, waiting and waking (`dev watch --wait`, the one wait for every devkit event), `dev time`, the message templates, status reports |
| [Continuity](continuity.md) | What devkit keeps of a session through compaction, resume, and `/clear`, the continuity brief, `dev session`, and `devkit_resume` in chat |
| [Dashboard](dashboard.md) | The live command center: `dev dashboard` and its server, Now (the view it opens on: does anything need you), the views, Background work, the crew, drawers, live and honest states, search, shortcuts, routes |
| [Git and GitHub](git-and-github.md) | `dev branch`, `commit`, `rebase`, `push`, `pr`, `run`, `release`, and `issue`: local CI evidence, the PR template, safe merges, releases by the policy |
| [Testing](testing.md) | `dev test`: failed first, then the blast radius of your changes; `dev test audit` and `dev test prove`: tests that can fail; `dev ci --run --resume` |
| [CI/CD pipelines](ci-cd.md) | `dev ci audit` and `dev ci init` on GitHub Actions, GitLab, Bitbucket, and Azure; `dev ci watch`: every run followed to the end, each result reported once, failures diagnosed; `dev ci watch --wait` and `dev ci watches`: a wait that wakes the agent |
| [Delivery](delivery.md) | The delivery policy, local-first GitOps (one integration branch, one push, one CI run), following CI with `dev ci status`, releases |
| [Lessons](lessons.md) | Learning from mistakes: recording, recalling, counting repeats, automating prevention, and `dev sleep` |
| [Scheduled jobs and the audit trail](jobs-and-audit.md) | `dev jobs`: the background jobs, their schedules, logs, and journal; `dev audit`: everything devkit changed or decided |
| [The audit trail](audit.md) | What each record holds (schema v2), levels, the hash chain and `dev audit verify`, queries and exports (JSONL, CSV, OCSF, CEF), retention, and the GDPR rights of the people recorded |
| [Diagrams](diagrams.md) | `dev diagram`: specs, validation, design rules, devkit's own diagrams |
| [Disk use](disk.md) | `dev du`: what fills your disk and what is safe to delete; Colima's disk and `dev docker trim`; `dev free` |
| [Disk and cleanup](disk-and-cleanup.md) | `dev gc` safety rules, automatic cleanup, `dev du`, agent data retention |
| [Docker](docker.md) | Colima, Compose, and Buildx, Testcontainers, amd64 images |

## Reference

| Guide | What it covers |
| --- | --- |
| [Commands](commands.md) | Every `dev` command and option: git and GitHub, `dev progress` and `dev crew`, lessons, jobs and audit, the file commands, `dev test` and `dev stubs`, `dev ci status`, `watch`, `audit`, and `init`, `dev delivery`, `dev ext`, `dev impeccable`, `dev update --check`, the `dcd` and `dwt` shell functions, and `dev completion` |
| [Configuration](configuration.md) | Every key of `~/.config/devkit/config.json`, personal agent rules, environment variables, the files that apply to everyone |
| [Agents](agents.md) | What devkit configures in Claude Code and Codex, the crew's agents and Codex roles, the global instructions, every hook, how devkit stays on |
| [Skills and plugins](skills-and-plugins.md) | The devkit plugin, required skills, the curated catalog and optional groups, impeccable and `dev impeccable`, who changes extensions with `dev ext` and how drift shows, and what was left out |
| [Models and tokens](models-and-tokens.md) | Models, effort, and subagents; the Lookup scout (Sparks); Codex profiles; the routing rules |
| [Templates](templates.md) | Plan, spec, spike, bug-fix, and ADR templates; task templates; the pull request template |
| [Guard rules](guard.md) | Every rule the guard enforces, per agent, including devkit's own state and the forged-environment rules, typing into a terminal session, and commands it cannot read, with example messages, and escape hatches |
| [Troubleshooting](troubleshooting.md) | Logs, common problems (among them pin refusals and rollback, a session that runs an older devkit, stale work in `dev doctor`, a CI result that never came, a command the guard cannot read, and a refused `tmux send-keys`), and how to reset |

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
