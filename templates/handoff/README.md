# Handoff

The single source of truth for the state of every project, managed by
[devkit](https://github.com/fireflysoftwaresolutions/devkit). It lives at `~/Development/.handoff`. The session hooks of Claude
Code and Codex, the `dev` CLI, and an hourly background job keep it current, so any session, yours or an agent's, on
any machine, starts from where the last one stopped.

## What is here

| Path | What | Maintained by |
| --- | --- | --- |
| `PROJECTS.md` | Live map: initiatives, then every project with status, tasks, branch, git state, worktrees, PRs, activity | `dev sync` |
| `TASKS.md` | Every open task across projects and initiatives (Now / Next / Later), blocked, and overdue items | `dev sync` |
| `ACTIVITY.md` | Timeline of sessions and operations over the last 30 days | `dev sync` |
| `registry.json` | The same data, machine-readable (`dev overview --json`, the dashboard) | `dev sync` |
| `projects/<project id>.md` | One handoff note per project (see below) | the project's sessions, `dev` |
| `initiatives/<slug>/` | Programs that span projects: `README.md` (goal, scope, workstreams, risks), plus `source.md`, `MIGRATION.md`, and `docs/` for adopted programs | member projects, `dev initiative` |
| `items/<scope>/<ID>.md` | Work items: one versioned file per task, feature, bug, or spike, which carries a lock and a changelog | `dev task` only |
| `plans/<scope>/` | Plans, specs, spikes, bug-fix plans and ADRs, versioned with a changelog | `dev plan`, agents |
| `sessions/<scope>/YYYY-MM.md` | Append-only session log: one line per session (who, machine, agent, branch, outcome) | hooks, `dev handoff --log` |
| `presence/<user>@<host>.json` | What each machine is doing now: live sessions, progress, held locks | `dev sync` on that machine |
| `log/YYYY-MM.md` | Operations: worktree removals, cleanups, adoptions, imports, restored settings | `dev` |

A scope is a project id or `initiatives/<slug>`.

## Developers and machines

- **Claim before you work.** `dev task start <id>` takes a lock on the item and pushes it at once. Other machines
  see it, and their agents are told not to work on it. Locks renew while a session works and expire when it stops.
- **Every change has a reason.** Changes of scope (edit, move, cancel) need `--why`, and every other change accepts
  one. Each change bumps the item's version, adds a changelog line (who, machine, agent, when, why), and makes one
  commit.
- **No merge conflicts.** Session logs merge as a union, generated views keep the local copy and are regenerated,
  and presence files are written only by their own machine.
- `dev team` shows who is working on what, and `dev report` gives a status report for the team.

A project id is the project's path under `~/Development`: `owner/repo` for GitHub, `<host>/<group>/<repo>` for other
hosts, `_local/<name>` for projects without a remote.

## A handoff note

```markdown
---
status: active          # active | paused | blocked | done | archived     (dev note set status=…)
priority: normal        # high | normal | low
milestone: v2.4 release
due: 2026-12-01
tags: [payments, api]
---
# owner/repo
<!-- dev:auto:start --> path, remote, stack, commands, branch, worktrees, PRs — generated <!-- dev:auto:end -->
## Purpose               what it is, who uses it, where it runs
## Current state         what works, what is half done, what is broken (rewrite, don't append)
## Tasks                 generated from the work items (dev task add|start|done)
## Decisions and gotchas non-obvious facts a newcomer would rediscover the hard way
## Links                 docs, environments, dashboards, related repositories
## Session log           (older notes) — new sessions go to sessions/<scope>/YYYY-MM.md
```

## Working with it

```bash
dev overview                          # every initiative and project at a glance (--json for tools and agents)
dev dashboard                         # the visual command center
dev task                              # work items of the project you are in (-i <slug> for an initiative)
dev task add "…" --type bug --why "…" --done "…" --now
dev task start|done|release <id>      # claim, finish, give back (the item's id, T-…)
dev task delegate <id> --to <login>   # hand it to a teammate, with the message to send
dev plan new "…" --kind spec          # plan | spec | spike | bugfix | adr;  dev plan revise <file> --why "…"
dev note set status=blocked           # priority=high milestone="…" due=YYYY-MM-DD tags=a,b
dev handoff --log "…"                 # a session line; dev handoff --edit opens the note
dev team · dev standup · dev report   # who is on what · what happened · status report
```

Never store secrets, tokens, or personal data here. Edit notes freely outside the generated blocks (`dev:auto`,
`dev:tasks`).
