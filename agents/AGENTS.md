# Global instructions (devkit)

These rules apply to every project and to both Claude Code and Codex. A repository's own AGENTS.md / CLAUDE.md
adds to them and wins on project specifics (build commands, style). The user's explicit request in the session
wins over both.

## 0. Instruction canary — `DevKit ·`

Start the **final message of every turn** with `DevKit ·` followed by a one-line status:
`DevKit · <project> · <what you did or found> · <state>` — for example
`DevKit · kit-consulting · integrated PRs 12/17/22 on release/26.11.4 · local suite green, ready to push once`.

This is how the user verifies that these global instructions are loaded and followed. devkit checks every reply
automatically; a reply without the marker is recorded as a deviation and reported to the user. Never omit it, never
fake compliance with the rest of these rules: if you could not follow a rule, say so in that line.

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
  change per commit. **Tags:** `vMAJOR.MINOR.PATCH` (semver), or the project's existing scheme (e.g. calendar
  versions).
- Commit only when the user asks or the task clearly includes it. Never push to `main`/`master` directly unless the
  user says so; never plain force-push (use `--force-with-lease` on your own branch only).
- Use `gh` for GitHub: PRs (`gh pr create --fill` then edit), reviews, checks (`gh pr checks`), CI logs.

## 7. Delivery: integrate and verify locally, push once, one CI run

GitHub CI is the confirmation, not the test runner. Every push and every merge costs a full CI cycle (often 30+
minutes) and can re-trigger the CI of every other open PR. Follow the `devkit-delivery` skill; the essentials:

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

Follow the `devkit-backend` skill for the detailed checklists. Always:

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

## 11. Handoff (keep the next session from starting cold)

Before you finish a substantial piece of work — and whenever the user says "handoff", "pause", "wrap up" or "stop
here" — update the project's handoff note with the `devkit-handoff` skill: current state, next steps, decisions and
gotchas, plus one summary line in the session log. Keep it short and true; rewrite stale text rather than appending.
devkit records the branch and commits of each session automatically.

## 12. The devkit guard

A pre-tool hook enforces the workspace rules above (layout, worktrees, clones, backups, destructive commands, force
pushes, attribution). When it blocks an action, read the reason and do it the way it describes. Do not try to work
around it with other commands; if the user really wants the blocked action, tell them what was blocked and why.
