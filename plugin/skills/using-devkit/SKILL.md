---
name: using-devkit
description: The operating manual for working inside a devkit workspace—the session protocol (start, during, finish), which `dev` command and which skill to use for each job, projects versus initiatives and who may write what in the handoff repository, and the [Firefly> DevKit] status block. Use at the start of any development session, whenever you are unsure which devkit tool or skill fits a task, and before you finish a turn that changed code, tasks or plans.
---

# Using devkit

devkit gives every session the same ground truth: the handoff repository (`~/Development/.handoff`). Read it at the
start, keep it true while you work, and record the outcome when you finish. Your global instructions hold the rules;
this skill is the how.

## Session protocol

### Start

1. Read the injected _devkit · workspace context_: project, branch, git state, commands, initiatives, Now tasks, last
   status and the handoff digest. It replaces exploration—do not rediscover it.
2. Pick up where the last session stopped: the first Now task, or what the user asked. `dev task` shows the board.
3. Unsure about the portfolio? `dev overview` (add `--json` to compute with it).

### During

- Start the item you work on, on its branch, before you work: `dev task start <n>` takes the lock, pushes it, and
  links the item to the work branch you are on (`dev branch new` and `dev wt new` link the one item you hold without a
  branch). The lock renews only while a session of yours is on that branch; elsewhere it runs out after 8 hours and
  the item goes back to todo. Never work on a task the context lists as claimed by someone else—pick another one or
  tell the user.
- Record work as it happens: `dev task add "…" --type task|feature|bug|spike --why "…" --done "<criterion>" --now
  --claim` for new work (it gets a structured description), `dev task done <n> --why "<evidence>"` only after
  verifying it, `dev task release <n>` if you stop without finishing, `dev task add "…" --later` for follow-ups you
  discover. Nothing stays doing without a lock: `dev pr merge` closes the merged branch's items, and
  `dev task reconcile` lists what drifted (`work-state` skill).
  Changes of scope need a reason: `dev task edit|move|cancel|block|reopen <n> --why "…"`. A closed item keeps the
  state it was closed in: `move`, `block`, and `cancel` need an open item, `unblock` a blocked one, and a done item
  with work left comes back with `dev task reopen <n> --why "<what is left>"`. Initiative-level work uses `-i`.
- Before you edit files outside your own lane, or assign a scope, run `dev crew owner <paths>`: it names the member
  whose step holds them. A member whose lane ended or went stale holds no files.
- Plans and specs go to `~/Development/.handoff/plans/<project id>/` (`dev plan new "…"`; Superpowers writes there
  too). After you change one, `dev plan revise <file> --why "…"`—the Stop hook insists. An approved plan whose text
  changed goes back to draft until it is approved again (`dev plan approve`).
- Work items, session logs and presence are written through `dev` only (versions, locks, history, attribution).
- Before pushing: `dev ci` (what CI runs) → run it locally → push once (`delivery` skill, `/devkit:ship`).
- Before a release: `dev task reconcile`, then close or reopen what it lists; `dev release` refuses open items of
  branches merged since the previous tag.

### Finish

1. Close or release the items you worked on, then update the project's note with the `handoff` skill (Current state,
   tasks, decisions); `dev handoff --log "…"` adds your one-line summary. devkit adds branch, commits, and your status
   block automatically.
2. Start your final message with
   the status block: `` **[Firefly> DevKit]** <project> · `<branch>` · <task> ``, then the bullets **Done:**,
   **Check:** (✓/⚠/✗ with the evidence) and **Next:** (global instructions §0 has the rules).

## Required skills

devkit knows which skills each kind of work needs (`config/skill-rules.json` in the devkit repo, extended by
`skills.rules` in the developer's config). Its rules follow what the work does, never the words of a prompt: the files
you change, the commands you run, and the kind of work a step or brief declares. devkit names the skills for the files
you change, and a crew brief carries those of its kind of work. Load them before you start, put them in every brief
you write, and expect the Stop hook to send you back once if you changed UI, Spring, migration, workflow, docs, or
diagram files without them.

## Which tool for which job

| Job | devkit | Skills |
| --- | --- | --- |
| Find, clone, start a project | `dev ls` (this Mac; `--all` adds the portfolio), `dev go`, `dev alias` (short names the team shares), `dev clone`, `dev new`, `dev adopt` (move a repository into the layout, with the developer), `dev open`; `dev claude` and `dev codex` start an agent in it | `workspace` |
| Parallel work | `dev wt new <type>/<topic>` | `workspace` |
| Plan, build, debug | `dev task`, plans folder | Superpowers (brainstorming, writing-plans, TDD, systematic-debugging) |
| Test | `dev test` (failed first, then the blast radius), `dev test plan`, `dev test audit`, `dev test prove`; `dev stubs` for unfinished work | `testing`, Superpowers TDD |
| CI/CD pipelines | `dev ci audit [--fix]`, `dev ci init`, `dev ci status`, `dev ci watch --wait` (the wait that wakes you: a Monitor or background command in Claude Code, the foreground in Codex), `dev ci watches` | `cicd`, `github-actions-hardening`, `devsecops` |
| Architecture and design | `dev initiative` for cross-project work | `architecture`, `system-design`, `domain-modeling`, `codebase-design`, `likec4-dsl`, `diagrams` |
| Backend | — | `backend`, `java-springboot`, `spring-boot-testing`, `java-junit` |
| Frontend | Chrome DevTools MCP, `playwright-cli`, `dev impeccable <verb>` (impeccable's engine: `context` once at the start, `detect`, …; never its launcher by path) | `angular-developer`, `react-best-practices`, `composition-patterns`, `frontend-design` / `impeccable` |
| Security | `dev sec scan` | `devsecops`, `/security-review` |
| Branch, commit, PR | `dev branch new`, `dev branch ls` (where each branch lives, backed up or not), `dev commit --why "…" --verified "…"` (the commit template), `dev rebase`, `dev push`, `dev backup` (a copy on the remote when it starts no CI run; `dev backup policy` shows the project's policy, `dev branch embargo` keeps a branch off), `dev pr create [--draft]\|ready\|checks --watch\|merge` (merge closes the branch's items and deletes the branch), `dev branch prune [--apply]` (merged branches here and on origin, by ancestry, PR, or content, with their worktrees; `--apply` deletes on origin, an irreversible action a crew member never runs), `dev run view --failed`, `dev issue`; `dev git` and `dev gh` for anything else | `delivery`, `communicating` (commit and PR templates) |
| Ship | `dev ci --run`, `dev ci status`, `dev calver`, `dev delivery`, `dev release`, `/devkit:ship` | `delivery`, `release-policy` |
| Document | `dev docs check`, `dev diagram render` | `docs`, `documentation-writer`, `doc-coauthoring`, `diagrams` |
| Hand off | `dev handoff`, `dev note set status=… priority=…`, `dev task`, `/devkit:handoff`; `dev handoff-import` adopts an older handoff repository; `dev session brief` (what a session kept through compaction or `/clear`), `dev session carry` (the run into this session after `/clear`) | `handoff` |
| Coordinate with others | `dev team`, `dev task start\|release`, `dev task reopen <n> --why "…"`, `dev task show <n>` (history) | — |
| Keep work state true | `dev task reconcile [--apply]` (doing items without a lock, open items of merged branches, duplicates), the work state row of `dev doctor`, `dev ci watches`, `dev branch prune [--apply]`, `/devkit:reconcile` (all of them, fixes after the developer agrees) | `work-state` |
| Choose model, effort, subagents | `sparks` agent (alias `scout`) for lookups; `codex --profile fast\|deep\|review` | `working-efficiently` |
| Lead the crew | `dev crew` (roster), `dev crew raci [<id>]`, `dev crew owner <paths>` (who holds those files, before you edit outside your lane or assign a scope), `dev progress assign <step> --to <member> --activity <id>`, `dev crew brief <member> "…" --activity <id>`, `dev crew graph` (the session as one graph), `dev crew next` (the slots and the order to launch in), `dev progress step --approves "<steps>"`, `dev progress confirm <step> --why "…"` (a step's work still holds after a reopen), `dev crew withdraw <id> --why "…"` (a decision that no longer applies), `dev crew rules` (every rule and its check) | `crew`, `work-state` |
| Learn from mistakes | `dev lesson add\|anchor\|recur\|prevent`, `dev lesson --search "…"`, `dev lesson scan\|review`; `dev sleep` consolidates them nightly | `learning-loop` |
| Keep the developer informed | `dev progress start\|step\|note\|block\|done`, `dev time` | `communicating` |
| Plans and specs | `dev plan new "…" --kind plan\|spec\|spike\|bugfix\|adr`, `dev plan revise --why`, `approve`, `ls` | Superpowers writing-plans, `communicating` (formats) |
| Hand work to a teammate | `dev task delegate <n> --to <login> --why "…"` | `communicating` |
| Report status | `dev report [--days 7] [-i <slug>]`, `dev standup` | `communicating` |
| Where things stand | `dev status`, `dev overview`, `dev standup`, `dev dashboard` (for the developer), `dev prompt` (the prompt helper; `dev setup prompt` adds it), `/devkit:status` | — |
| Something is off | `dev doctor`, `dev canary` (status block results and the turns the Stop hook sent back), `dev agents` (both agents' versions, sign-in, quota), `dev config diff`, `dev guard check "<cmd>"`, `dev plugin validate` | — |
| Background jobs and history | `dev jobs` (ls, run, set, logs), `dev audit`, `dev sync` (the handoff repository) | — |
| Third-party plugins, skills, and MCP servers: what is installed, what pins it, why it is there, what is outdated | `dev ext ls [--agent claude\|codex] [-v] [--json]`, `dev ext outdated [<name>]`, `dev ext update <name> --why "…"` (a team pin, on a branch; `--apply` is the developer's), `dev ext add\|remove --team` (devkit's defaults, on a branch); `dev ext add\|remove\|enable\|disable` and `dev ext ack` (confirms the acceptances listed at the session start) are the developer's | `devsecops` |
| Files, safely | `dev files`, `dev find`, `dev grep`, `dev diff`, `dev rm` (to the Trash, `--undo`), `dev mv`, `dev cp` | — |
| Disk and Docker | `dev du` (what fills the disk, why, and the plan), `dev free <n>` (frees a plan item, with the merge test of `dev branch prune`; `dev gc` is its old name), `dev docker up\|status\|trim` | `workspace` |
| Install, update, or roll back devkit, and change its settings (the developer, in a terminal: the guard refuses them to agents) | `dev setup`, `dev setup prompt`, `dev setup commit-template`, `dev setup shell` (the zsh helpers), `dev completion zsh\|bash` (Tab completion, generated from the parser), `dev install [--only <steps>]`, `dev install --version <previous>` (rollback), `dev update` (`dev update --check` only tells, and agents may run it; both list the running sessions that use devkit, which the developer restarts after), `dev config set <key> <value>`, `dev version` | — |
| Claude Desktop chat and Cowork | `dev desktop status\|pack`; the extension runs `dev mcp` | — |

## Projects and initiatives

- A **project** is one repository: its note is `projects/<project id>.md`. A session works on one project and writes
  only that project's note and plans.
- An **initiative** (`initiatives/<slug>/README.md`) spans more than one project—an integration, a platform, a program.
  It holds the goal, scope, one workstream per member project, cross-project tasks, decisions, and contracts. Member
  projects update it; non-members don't.
- Facts that concern another project go to a shared initiative, or you tell the user—you never edit another
  project's note. The guard enforces this; a portfolio-level session (started in `~/Development` or the handoff repo)
  may work across projects when the user asks.
- Generated files (`PROJECTS.md`, `TASKS.md`, `ACTIVITY.md`, `registry.json`) are never edited by hand.

## The status block

It is how the user sees, reply by reply, that you follow these instructions—and it feeds the project's latest
status, the session log, the dashboard, and the next session. Make it specific and true: name the branch, the outcome,
the evidence (`tests green`, `CI not run`, `not verified`) and the next step. If a rule could not be followed, say so
there.
