---
name: using-devkit
description: The operating manual for working inside a devkit workspace — the session protocol (start, during, finish), which `dev` command and which skill to use for each job, projects versus initiatives and who may write what in the handoff repository, and the [Firefly> DevKit] status block. Use at the start of any development session, whenever you are unsure which devkit tool or skill fits a task, and before you finish a turn that changed code, tasks or plans.
---

# Using devkit

devkit gives every session the same ground truth: the handoff repository (`~/Development/.handoff`). Read it at the
start, keep it true while you work, and record the outcome when you finish. Your global instructions hold the rules;
this skill is the how.

## Session protocol

**Start**
1. Read the injected *devkit · workspace context*: project, branch, git state, commands, initiatives, Now tasks, last
   status and the handoff digest. It replaces exploration — do not rediscover it.
2. Pick up where the last session stopped: the first Now task, or what the user asked. `dev task` shows the board.
3. Unsure about the portfolio? `dev overview` (add `--json` to compute with it).

**During**
- Claim before you work: `dev task start <n>` takes the lock and pushes it, so other developers' agents see it.
  Never work on a task the context lists as claimed by someone else — pick another one or tell the user.
- Record work as it happens: `dev task add "…" --type task|feature|bug|spike --why "…" --done "<criterion>" --now
  --claim` for new work (it gets a structured description), `dev task done <n>` only after verifying
  it, `dev task release <n>` if you stop without finishing, `dev task add "…" --later` for follow-ups you discover.
  Changes of scope need a reason: `dev task edit|move|cancel|block <n> --why "…"`. Initiative-level work uses `-i`.
- Plans and specs go to `~/Development/.handoff/plans/<project id>/` (`dev plan new "…"`; Superpowers writes there
  too). After you change one, `dev plan revise <file> --why "…"` — the Stop hook insists.
- Work items, session logs and presence are written through `dev` only (versions, locks, history, attribution).
- Before pushing: `dev ci` (what CI runs) → run it locally → push once (`delivery` skill, `/devkit:ship`).

**Finish**
1. Update the project's note with the `handoff` skill (Current state, tasks, decisions); `dev handoff --log "…"` adds
   your one-line summary. devkit adds branch, commits and your status block automatically.
2. Start your final message with
   the status block: `` **[Firefly> DevKit]** <project> · `<branch>` · <task> ``, then the bullets **Done:**,
   **Check:** (✓/⚠/✗ with the evidence) and **Next:** (global instructions §0 has the rules).

## Which tool for which job

| Job | devkit | Skills |
|---|---|---|
| Find, clone, start a project | `dev ls`, `dev go`, `dev clone`, `dev new` | `workspace` |
| Parallel work | `dev wt new <type>/<topic>` | `workspace` |
| Plan, build, debug | `dev task`, plans folder | Superpowers (brainstorming, writing-plans, TDD, systematic-debugging) |
| Architecture and design | `dev initiative` for cross-project work | `architecture`, `system-design`, `domain-modeling`, `codebase-design`, `likec4-dsl`, `diagrams` |
| Backend | — | `backend`, `java-springboot`, `spring-boot-testing`, `java-junit` |
| Frontend | Chrome DevTools MCP, `playwright-cli` | `angular-developer`, `react-best-practices`, `composition-patterns`, `frontend-design` / `impeccable` |
| Security | `dev sec scan` | `devsecops`, `/security-review` |
| Ship | `dev ci`, `dev calver`, `/devkit:ship` | `delivery` |
| Document | `dev docs check`, `dev diagram render` | `docs`, `documentation-writer`, `doc-coauthoring`, `diagrams` |
| Hand off | `dev handoff`, `dev task`, `/devkit:handoff` | `handoff` |
| Coordinate with others | `dev team`, `dev task start|release`, `dev task show <n>` (history) | — |
| Choose model, effort, subagents | `scout` agent for lookups; `codex --profile fast|deep|review` | `working-efficiently` |
| Keep the developer informed | `dev progress start|step|block|done`, `dev time` | `communicating` |
| Plans and specs | `dev plan new "…" --kind plan|spec|spike|bugfix|adr`, `dev plan revise --why`, `approve`, `ls` | Superpowers writing-plans, `communicating` (formats) |
| Hand work to a teammate | `dev task delegate <n> --to <login> --why "…"` | `communicating` |
| Report status | `dev report [--days 7] [-i <slug>]`, `dev standup` | `communicating` |
| Where are we | `dev status`, `dev overview`, `dev standup`, `/devkit:status` | — |
| Something is off | `dev doctor`, `dev config diff`, `dev guard check "<cmd>"` | — |

## Projects and initiatives

- A **project** is one repository: its note is `projects/<project id>.md`. A session works on one project and writes
  only that project's note and plans.
- An **initiative** (`initiatives/<slug>/README.md`) spans several projects — an integration, a platform, a programme.
  It holds the goal, scope, one workstream per member project, cross-project tasks, decisions and contracts. Member
  projects update it; non-members don't.
- Facts that concern another project go to a shared initiative, or you tell the user — you never edit another
  project's note. The guard enforces this; a portfolio-level session (started in `~/Development` or the handoff repo)
  may work across projects when the user asks.
- Generated files (`PROJECTS.md`, `TASKS.md`, `ACTIVITY.md`, `registry.json`) are never edited by hand.

## The status block

It is how the user sees, reply by reply, that you follow these instructions — and it feeds the project's latest
status, the session log, the dashboard and the next session. Make it specific and true: name the branch, the outcome,
the evidence (`tests green`, `CI not run`, `not verified`) and the next step. If a rule could not be followed, say so
there.
