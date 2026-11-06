# Handoff

Private tracking repository managed by [devkit](https://github.com/ancongui/devkit). It lives at
`~/Development/.handoff` and is updated automatically — by the agents' session hooks and by an hourly background
job — so any session (yours, Claude Code's or Codex's, on any of your machines) starts from the current state.

| Path | What | Written by |
|---|---|---|
| `PROJECTS.md` | Live map: every project, branch, state, worktrees, PRs, activity, size, what needs attention | `dev sync` |
| `registry.json` | The same map, machine-readable | `dev sync` |
| `projects/<owner>/<repo>.md` | One handoff note per project: purpose, current state, next steps, decisions, session log | agents (devkit `handoff` skill) + `dev sync` (auto block, session commits) |
| `plans/<owner>/<repo>/` | Specs and implementation plans (kept out of the code repositories) | agents |
| `log/YYYY-MM.md` | Activity log: worktree removals, cleanups, adoptions | `dev gc`, `dev adopt` |

Never store secrets here. Edit notes freely outside the `dev:auto` blocks.
