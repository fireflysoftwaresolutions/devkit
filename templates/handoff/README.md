# Handoff

The single source of truth for the state of every project, managed by
[devkit](https://github.com/fireflysoftwaresolutions/devkit). It lives at `~/Development/.handoff` and is kept up to
date automatically — by the session hooks of Claude Code and Codex, by the `dev` CLI and by an hourly background job —
so any session, yours or an agent's, on any of your machines, starts from where the last one stopped.

## What is here

| Path | What | Maintained by |
|---|---|---|
| `PROJECTS.md` | Live map: every project with status, tasks, branch, git state, worktrees, PRs, activity, size; what needs attention | `dev sync` |
| `TASKS.md` | Every open task across projects (Now / Next / Later), blocked and overdue items | `dev sync`, from the notes |
| `ACTIVITY.md` | Timeline of sessions and operations over the last 30 days | `dev sync` |
| `registry.json` | The same data, machine-readable (used by `dev overview --json` and the dashboard) | `dev sync` |
| `projects/<project id>.md` | One handoff note per project (see below) | agents, you, and `dev` |
| `plans/<project id>/` | Specs and implementation plans, kept out of the code repositories | agents, you |
| `log/YYYY-MM.md` | Operations: worktree removals, cleanups, adoptions, restored settings | `dev` |

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
## Tasks                 ### Now / ### Next / ### Later / ### Done    (dev task add|start|done)
## Decisions and gotchas non-obvious facts a newcomer would rediscover the hard way
## Links                 docs, environments, dashboards, related repositories
## Session log           newest first; one line per session with branch, commits and the agent's DevKit status line
```

## Working with it

```bash
dev overview                     # every project at a glance (--json for tools and agents)
dev dashboard                    # the visual command centre
dev task                         # tasks of the project you are in
dev task add "…" --now           # dev task start|done|next|later <n>
dev note set status=blocked      # priority=high milestone="…" due=YYYY-MM-DD tags=a,b
dev handoff --edit               # open the note; dev handoff --log "…" adds a session line
dev standup                      # what happened since yesterday
```

Never store secrets, tokens or personal data here. Edit notes freely outside the `dev:auto` block.
