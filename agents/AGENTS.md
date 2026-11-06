# Global instructions (devkit)

These rules apply to every project and to both Claude Code and Codex. A repository's own AGENTS.md / CLAUDE.md
adds to them and wins on project specifics (build commands, style). The user's explicit request in the session
wins over both.

## 0. Instruction canary and status line — `DevKit ·`

Start the **final message of every turn** with one status line:

`DevKit · <project>@<branch> · <what you did or found> · <verification> · next: <next step>`

for example `DevKit · kit-consulting@release/26.11.4 · integrated PRs 12/17/22 · local suite green · next: push once,
open the release PR`. Use `-` for a part that does not apply (`DevKit · - · answered a question · - · next: -`).

It proves to the user that these global instructions are loaded and followed (devkit checks every reply and reports a
missing marker as a deviation), and it is useful: devkit stores the line as the project's latest status, adds it to
the session log of the handoff note, shows it on the dashboard and gives it to the next session. Keep it truthful: if
you could not follow a rule or something failed, say so in that line.

## 1. Workspace: where everything lives

- `{{DEV}}/<owner>/<repo>` — GitHub repositories (lowercase). `dev clone <owner/repo>`.
- `{{DEV}}/<host>/<group>/<…>/<repo>` — any other git host: `gitlab.com/group/subgroup/repo`,
  `bitbucket.org/workspace/repo`, `gitlab.mycompany.com/team/service`. `dev clone <url>`.
- `{{DEV}}/_local/<name>` — projects with no remote yet. `dev new <name>`.
- `{{DEV}}/.worktrees/<project path>/<branch>` — managed worktrees. `dev wt new <branch>`.
- `{{DEV}}/.handoff/` — the user's private tracking repo: `PROJECTS.md` (live map), `projects/<owner>/<repo>.md`
  (one handoff note per project), `plans/<owner>/<repo>/` (specs and plans), `log/` (activity). Synced automatically.
- Scratch and temporary files: your session scratchpad if the harness gives you one, otherwise `{{DEV}}/.tmp/`.
  Never in the repository, never in `~`, never next to the repository.

Never create sibling copies of a repository (`repo-pr12`, `repo-copy`, `repo-ci`), never clone outside this layout,
never move or rename repositories yourself (`dev adopt` does that, with the user).

## 2. Start of every task

1. Read the **devkit workspace context** injected at session start: project, branch, state, commands, other
   worktrees and the handoff note. Do not rediscover what it already tells you.
2. If the handoff note has a "Next steps" section that matches the request, continue from it.
3. Read the repository's own instructions (AGENTS.md, CLAUDE.md, CONTRIBUTING.md) before changing code.
4. Find code with targeted searches (symbol names, file globs, LSP "go to definition"/"find references") instead of
   reading whole directories. Stop exploring as soon as you can act.

## 3. How to work

- **Scale the process to the task.** A one-line fix: just do it and verify. A feature or a change across several
  files: write a short plan first (goal, files, steps, how you will verify) and follow it. Ambiguous requirements:
  ask one focused question instead of guessing.
- **Bugs: reproduce first.** Write or run a failing test (or a minimal reproduction) before fixing, then confirm it
  passes. Find the root cause; do not paper over symptoms.
- **Small, verified steps.** After each meaningful change run the narrowest relevant check (one test file, the type
  checker for the touched package), and the full suite before you say the work is done.
- **Evidence before claims.** Never say "done", "fixed" or "tests pass" without having run the command in this
  session. If something fails or was skipped, say so plainly with the output.
- **Follow the codebase.** Match existing patterns, naming, libraries and structure. Do not introduce a new
  dependency, framework or architectural pattern without the user's agreement.
- **Stay in scope.** Do what was asked. Note unrelated problems in your reply or the handoff note; do not fix them
  silently.

### Write plainly, in American English

Everything you write (replies, plans, commits, PRs, docs, messages for the team) is in **American English** unless the
developer asks for another language. It is direct and simple: lead with the point, use short sentences and everyday
words, explain each acronym once, and give concrete facts and numbers. Drop buzzwords, filler and anything that does not change
what the reader understands or does. Templates for PR descriptions, merge messages, review comments and messages
that hand work to a teammate (`dev task delegate`) are in the `communicating` skill.

### Keep the developer informed, and keep time predictable

The developer should never have to ask "how is it going?". Follow the `communicating` skill (it has the templates):

- **Kickoff.** For work longer than a few minutes: `dev progress start "<goal>" --steps N --estimate 30m`, then post
  a short plan with the estimate. The command shows how long similar work really took here; use that number.
- **During the work.** After each step, every ~8–10 minutes of tool work and whenever background work finishes:
  `dev progress step "<done, with evidence>" --next "<next>"`, then post the update it prints (≤ 5 lines: done · now ·
  next · ETA). The hooks remind you after a long silence.
- **Background work.** When you launch subagents, parallel sessions or long commands, say what runs, what it will
  produce and when you expect it. Relay each result when it lands. Never sleep or poll.
- **Decisions.** Ask only when the decision is the developer's (scope, behaviour, cost, risk, irreversible or external
  effects). Use the decision template: context, options with trade-offs, your recommendation, and what you will do if
  they don't answer. Explanations lead with the answer, then how it works, an example from their code, and what it
  means for them.
- **Time.** At 1.5× the estimate, re-plan and say so. Be fast without cutting corners: run independent tool calls in
  parallel, run long jobs in the background, run the narrowest check first and the full suite once. Use compact
  output and targeted reads, and never re-read what you have. `dev time` shows where time goes.
- **End.** The final report gives the outcome, what changed, the evidence, what is not done and the next step, after
  the DevKit status line. Then run `dev progress done "<outcome>"`, which notifies the developer and calibrates future
  estimates.

## 4. Files and disk

- Prefer editing existing files. Create a new file only when the change needs it.
- Do not create summary, report, notes or "PLAN.md" files in the repository unless the user asks. Put explanations in
  your reply, follow-ups in the handoff note, specs/plans in `{{DEV}}/.handoff/plans/<owner>/<repo>/`.
- No backup copies (`*.bak`, `*_old.*`, `*.orig`, `file copy.ts`): git is the history.
- Never commit build output, dependencies, secrets or local config (`dist/`, `node_modules/`, `.env*`, keys).
- Install dependencies with the project's own tool and lockfile; do not switch package managers. Never reinstall
  dependencies "just in case". Prefer `uv` for Python and `pnpm` for new Node projects (shared stores, little disk).
- Delete scratch files you created when you no longer need them.

## 5. Worktrees

- Default: work in the current checkout, on a branch.
- Create a worktree only when the user asks or when work must run in parallel with another session on the same
  repository. Use `dev wt new <type>/<topic>` (or your harness's own worktree feature) — never raw
  `git worktree add`; the devkit guard blocks unmanaged paths.
- One worktree per task, named after the branch. When its PR is merged, it is cleaned up automatically
  (`dev gc`); remove it yourself with `dev wt rm <branch>` when you abandon the task.
- Never edit another worktree's files from your session, and never use bare `git stash` / `git stash pop` (the stash
  is shared by all worktrees); use a WIP commit instead.

## 6. Git and GitHub

- The git identity is the user's GitHub identity (configured by devkit). Never change git config.
- **No AI attribution, ever:** no `Co-Authored-By: Claude/Codex` trailers, no "Generated with …" lines in commits,
  PRs, issues or code comments.
- **Branching model (trunk-based GitOps):** `main` is protected and always releasable; every change goes through a
  short-lived branch from an up-to-date `origin/main` and a PR; rebase on `main` before review; delete the branch
  after merge. `develop` exists only if the repository already uses it.
- **Branch names:** `<type>/<ticket>-<topic>` — lowercase kebab-case topic, optional ticket key, ≤ 60 characters.
  Types: `feat` `fix` `refactor` `perf` `docs` `test` `build` `ci` `chore` `deps` `style` `revert` for changes;
  `hotfix/<version-or-topic>` for urgent production fixes; `release/<version>` and `integration/<topic>` for local
  integration (section 7). Examples: `feat/PROJ-142-invoice-export`, `fix/login-timeout`, `release/26.11.4`.
  Never name a branch after a tool or agent (`claude/…`, `codex/…`, `ai/…`, `worktree-…`) or with a random id. If a
  harness created such a branch for you (Claude Desktop, Codex worktrees, detached HEAD), rename it before the first
  commit: `git branch -m <type>/<topic>`. The guard refuses to create or push non-conforming names.
- **Commits:** Conventional Commits `type(scope): summary` (imperative, ≤ 72 chars), body explains why; one logical
  change per commit.
- **Versions:** keep the project's existing scheme. Projects without one use **CalVer `YY.MM.PATCH`** — `26.11.0` is
  the first release of November 2026, `26.11.1` the next one that month, `26.12.0` the first in December. Tags are
  `vYY.MM.PATCH`, release branches `release/YY.MM.PATCH`. `dev calver` prints the current and the next version.
- Commit only when the user asks or the task clearly includes it. Never push to `main`/`master` directly unless the
  user says so; never plain force-push (use `--force-with-lease` on your own branch only).
- Use `gh` for GitHub: PRs (`gh pr create --fill` then edit), reviews, checks (`gh pr checks`), CI logs.

## 7. Delivery: integrate and verify locally, push once, one CI run

GitHub CI is the confirmation, not the test runner. Every push and every merge costs a full CI cycle (often 30+
minutes) and can re-trigger the CI of every other open PR. Follow the devkit `delivery` skill (`/devkit:ship`); the essentials:

1. **Know what CI runs.** `dev ci` lists the workflow jobs and their commands. Run those same commands locally
   (lint, type-check, unit, integration with Testcontainers, build, package) before any push.
2. **Several branches or PRs that must land together → one local integration branch.** Create
   `release/<version>` or `integration/<topic>` from an up-to-date `origin/<default>`, merge or rebase the branches
   into it locally (worktrees share the same repository, so no other session needs to push), resolve conflicts,
   bump version / CHANGELOG / docs once, and run the full local suite on the result.
3. **Push once, open one PR.** Only after the whole local suite is green. The PR closes or supersedes the
   individual PRs. Do not merge PRs one after another to "see what CI says".
4. **Watch, don't poll:** `gh pr checks --watch` or `gh run watch`.
5. **If CI fails:** read the failing log (`gh run view <id> --log-failed`), reproduce it locally, fix, re-run the
   local suite, then push once. Never chain speculative "try this" pushes. Only a failure that truly cannot be
   reproduced on macOS (Linux-only paths, CI secrets, runner limits) justifies a CI-only fix, and you say so.
6. **Do not blindly re-run CI.** `gh run rerun --failed` is only for a failure proven to be flaky; record the flaky
   test in the handoff note.
7. **Release from the verified integration branch:** merge with the repository's strategy, tag, publish, then
   verify the published artefact (download/install it) before calling the release done.
8. **GitOps:** git is the source of truth for code, configuration and infrastructure. Changes to environments go
   through PRs and tags; no manual changes to deployed systems, no drift.

## 8. Backend methodology

Follow the devkit `backend` skill for the detailed checklists. Always:

- **Contract first:** define or update the API contract (OpenAPI, GraphQL schema, message schema) before the
  implementation; keep changes backward compatible or version them; errors as RFC 9457 problem details when the API
  is HTTP/JSON and the project has no other convention.
- **Domain logic isolated** from frameworks, transport and persistence (follow the project's layering: hexagonal,
  layered, modules). No business logic in controllers, entities exposed directly, or SQL in handlers.
- **Database changes are migrations** (Flyway/Liquibase, Alembic, Doctrine/Laravel migrations): forward-only, never
  edit an applied migration, expand → migrate → contract for zero-downtime, data migrations separate from schema.
- **Tests:** unit tests for domain rules; integration tests against real dependencies with Testcontainers (no
  in-memory database stand-ins when the project uses Postgres/MySQL); contract tests for public APIs; deterministic
  tests (no sleeps, no order dependence, fixed clocks).
- **Correctness under load and failure:** explicit transaction boundaries, idempotent handlers and retries, timeouts
  and bounded retries on every outbound call, pagination on list endpoints, no N+1 queries.
- **Security by default:** validate input at the boundary, parameterised queries only, authorisation checked per
  operation, secrets from environment/secret manager (never in code, logs or tests), least privilege.
- **Operability:** structured logs with correlation ids and no personal data, health/readiness endpoints, metrics
  and traces where the project has them, configuration through environment (12-factor).

## 9. Skills and tools

- **Superpowers** (brainstorming, writing-plans, executing-plans, TDD, systematic-debugging,
  verification-before-completion): use them for non-trivial work. Save specs and plans to
  `{{DEV}}/.handoff/plans/<owner>/<repo>/`, not `docs/superpowers/` in the repository, and do not commit them
  unless the user asks. Its worktree step follows section 5: ask before creating one.
- **Library and framework docs:** prefer the vendor's version-matched source (Angular CLI MCP, the project's
  installed `node_modules/<pkg>` docs, framework MCPs), then **context7**, then web search. Check the version in the
  lockfile before using an API.
- **Code intelligence:** use the LSP tools (TypeScript, Python, Java, PHP) to find definitions, references and type
  errors rather than grepping when they are available.
- **Design and architecture:** `grill-with-docs`, `domain-modeling`, `codebase-design`,
  `improve-codebase-architecture`, `architecture`, `system-design`, `likec4-dsl` (C4 diagrams). ADRs and glossaries go
  in the repository only if it already keeps them (for example `docs/adr/`); otherwise ask where.
- **Frontend:** `frontend-design` (Claude) / `impeccable` (Codex) for UI quality; `angular-developer`,
  `react-best-practices`, `composition-patterns`; Chrome DevTools MCP and `playwright-cli` to verify in a real
  browser. Do not create PRODUCT.md / DESIGN.md unless the user asks.
- **Documentation and diagrams:** the devkit `docs` skill (README anatomy, Diátaxis, Google style, `dev docs check`)
  with `documentation-writer` and `doc-coauthoring`; the devkit `diagrams` skill for every diagram (SVG via
  `dev diagram render`, validated with `dev diagram check`, dark and light). Update docs in the same branch as the code.
- **Security (DevSecOps):** the devkit `devsecops` skill — threat model features that touch auth, data, money,
  files or infrastructure; `dev sec scan` (secrets, dependencies, SAST, filesystem and IaC) before a release and after
  dependency changes; least-privilege, SHA-pinned GitHub Actions. The guard refuses commits that contain secrets.
- **Reviews:** before saying a substantial change is ready, run the built-in review (`/code-review` in Claude Code,
  `/review` in Codex) or a security review for auth, input handling and secrets.

## 10. Stack conventions (when the repository does not say otherwise)

- **Java / Spring Boot:** use the wrapper (`./mvnw`, `./gradlew`); constructor injection; tests with JUnit 5 +
  AssertJ; Testcontainers for integration tests (Docker runs on Colima; `DOCKER_HOST` is set).
- **Python:** `uv` (`uv sync`, `uv run …`); type hints; `ruff` for lint/format; `pytest`. Never `pip install` into
  the system or Homebrew Python.
- **PHP:** Composer scripts; PSR-12; PHPUnit or Pest; PHPStan/Psalm at the level the project uses. Laravel: `php
  artisan`; Symfony: `bin/console`.
- **TypeScript / Node:** strict types, no `any` without a reason; run `tsc --noEmit` and the linter on what you touch;
  use the package manager the lockfile implies.
- **Angular:** standalone components, signals, the new control flow (`@if`, `@for`), `inject()`, OnPush; generate
  with the Angular CLI; follow the project's version (check `package.json`).
- **React / Next.js:** function components and hooks, server components where the app uses them, no unnecessary
  effects or state; follow the project's data-fetching and styling conventions.
- **Containers:** `dev docker up` starts Docker (Colima). Never prune volumes; never pull images or start long-running
  containers the task does not need, and stop the ones you started.

## 11. Projects, initiatives, tasks and handoff (keep the next session from starting cold)

The handoff repository is the single source of truth for the state of all work. The hierarchy:
**portfolio → initiatives (cross-project programmes, integrations, platforms; optional) → projects (one repository
each) → tasks (Now / Next / Later / Done), plans and sessions.** The devkit `using-devkit` skill is the full manual.

- **One project per session.** Write only your project's note (`projects/<project id>.md`) and plans
  (`plans/<project id>/`). Never edit another project's note: put cross-project facts in a shared initiative you are a
  member of, or tell the user. Initiatives (`initiatives/<slug>/README.md`) are updated only by member projects.
  `PROJECTS.md`, `TASKS.md`, `ACTIVITY.md`, `registry.json` and `log/` are generated: never edit them. The guard
  enforces all of this; portfolio-level sessions (started in `~/Development` or the handoff repo) may work across
  projects when the user asks.
- **Initiatives** hold the goal, scope, one workstream per member project, cross-project tasks, decisions and
  contracts (APIs, events, versions) every member must respect. `dev initiative ls|show`, `dev task -i <slug>`.

- **Team protocol (several developers, laptops and agents share the handoff repo):** claim a task before you work on
  it (`dev task start <n>` — the lock is pushed at once), never work on a task claimed by someone else (the session
  context lists them), and finish with `dev task done <n>` or `dev task release <n>`. Changes of scope (edit, move,
  cancel, block) need `--why "…"`: the reason is versioned in the item's changelog and its commit. After editing a
  plan or spec run `dev plan revise <file> --why "…"`. Work items, session logs and presence are written only through
  `dev`, never by hand. `dev team` shows who is working on what.

Use it, keep it current:

- `dev overview` — every project: status, priority, Now tasks, latest status line, last session (`--json` for tools).
- `dev status` / `dev task` — the current project and its work items; `dev task add "…" --now|--later
  [--type feature|bug|spike] [--why "…"]`, `dev task start|done|release <n>`, `dev task show <n>` (with history);
  `dev note set status=blocked priority=high milestone="…" due=YYYY-MM-DD`; `dev plan new|revise|approve`.
- `dev standup` — what happened recently; `dev dashboard` — the visual command centre (for the user).
- Slash commands in Claude Code: `/devkit:status`, `/devkit:overview`, `/devkit:task`, `/devkit:handoff`,
  `/devkit:standup`, `/devkit:ship`, `/devkit:ci`, `/devkit:doctor` (Codex has the same as skills).

Record work as it happens: add a task when you discover follow-up work, `start` it when you begin, `done` it when it is
verified. Plans and specs go to `{{DEV}}/.handoff/plans/<project id>/`.

### Handoff

Before you finish a substantial piece of work — and whenever the user says "handoff", "pause", "wrap up" or "stop
here" — update the project's handoff note with the devkit `handoff` skill (`/devkit:handoff`): current state, next steps, decisions and
gotchas, plus one summary line in the session log. Keep it short and true; rewrite stale text rather than appending.
devkit records the branch and commits of each session automatically.

## 12. The devkit guard

A pre-tool hook enforces the workspace rules above (layout, worktrees, clones, backups, destructive commands, force
pushes, attribution). When it blocks an action, read the reason and do it the way it describes. Do not try to work
around it with other commands; if the user really wants the blocked action, tell them what was blocked and why.
