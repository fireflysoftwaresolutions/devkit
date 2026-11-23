# Skills and plugins

This page lists what devkit installs into Claude Code and Codex, why each item is there, how devkit makes sure the
right skills are used for each kind of work, and what was left out on purpose.

devkit installs a **curated** set, not everything available. Every enabled plugin, skill, and MCP server costs context
in each session, and overlapping packs compete to trigger. Both agents already ship code review, simplification,
security review, plan mode and worktrees, so devkit does not duplicate them.

Selection criteria: maintained and widely used, an official vendor source where one exists, a clear license, no
uncontrolled telemetry or unpinned downloads, and no behavior that conflicts with the workspace rules.

`dev ext ls` shows every one of them on your Mac: its source, what pins it, whether it is installed and enabled, its
license, and why it is there (`-v`). The reason and license live next to each entry: `meta` in
`config/claude/plugins.json` and `config/codex/plugins.json`, and `why` in each source of `config/skills.json`.

## The devkit plugin

devkit ships its own plugin, `devkit@firefly-devkit`, for both agents. Its description is the tagline, "Every repo in
its place, every agent on the same page." It carries the devkit icon in both agents. Claude Code's manifest names the
icon and the display name, and Codex's adds the logo and the Firefly amber `#FFB34A` as its brand color.
`dev plugin validate` checks it with Claude Code's validator and Codex's own parser, and `dev install` reinstalls an
agent's copy when it differs from the checkout.

| Skill | Use |
| --- | --- |
| `using-devkit` | The operating manual: the session protocol, which `dev` command and skill fits each job, projects versus initiatives, the status block |
| `configuration` | Strict settings, fresh diagnostics, transactional edits, migration, and concurrent configuration writers |
| `handoff` | Update the project's handoff note before finishing, or when you say `handoff`, `pause`, or `wrap up` |
| `communicating` | Templates for every message: kickoff, progress, status, decision, explanation, final report, failure, PR description, merge message, review comments, delegation, and the structured formats (see [Progress and communication](progress-and-communication.md#how-agents-write-to-you)) |
| `working-efficiently` | Models, effort, subagents, and token hygiene (see [Models and tokens](models-and-tokens.md)) |
| `workspace` | Layout, `dev` commands, worktrees, disk, Docker, per-project vendor tooling |
| `delivery` | Local-first GitOps: integration branch, one push, one CI run, CI failures, releases |
| `release-policy` | The delivery policy: detect, recommend, and record versioning, bumps, the test gate, the changelog, and the release flow with `dev delivery`; release trains |
| `learning-loop` | Learn from mistakes: record a blameless lesson after a fix, a revert, a repeated refusal, or a correction; recall the known pitfalls; count repeats; turn recurring lessons into automation (see [Lessons](lessons.md)) |
| `root-cause` | Find and prove the root cause before any fix: reproduce, check the known lessons and devkit bugs, prove one cause with an experiment, fix it, verify, and record it (`dev commit --cause`); the 5-whys check, when to escalate, and when to bring in the Performance analyst or the Incident commander (see [The crew › How work flows](crew.md#how-work-flows)) |
| `research` | Research a technology from version-matched sources: pin the version from the lockfile, primary sources first, a version, link, and date on every claim, and a spike note the next session finds; re-checked after 90 days or a new major version |
| `functional-analysis` | The Business analyst's analysis: actors and goals, use cases, numbered business rules, the edge-case checklist, non-functional needs with numbers, Given/When/Then acceptance criteria with ids, and a test for each id |
| `backend` | API contracts, layering, migrations, testing, resilience, security, operability; notes for Spring, Python, PHP, Node, .NET, Go, Rust, and Ruby |
| `mobile` | iOS and Android apps: platform conventions, offline behavior, permissions and privacy declarations, security, device tests, signing, and store-ready builds |
| `devsecops` | Threat modeling, secrets, dependencies and supply chain, SAST, CI hardening, containers and infrastructure, and `dev sec scan` |
| `docs` | Documentation to the devkit standard: Diátaxis, Google style, American English, `dev docs check` |
| `diagrams` | Branded, validated SVG diagrams with `dev diagram` |
| `testing` | Tests that can fail for a real bug, a strategy per kind of code, TDD done right, running only what a change can break (`dev test`), flaky tests, mutation testing, diagnosing an existing suite (see [Testing](testing.md)) |
| `cicd` | Fast, reliable, DevSecOps pipelines on GitHub Actions, GitLab, Bitbucket and Azure: `dev ci audit` and `dev ci init` (see [CI/CD pipelines](ci-cd.md)) |
| `crew` | Leading devkit's crew: who to send, parallel or in sequence, briefs, lanes, scopes, messages, delegation, guided or automatic decisions (see [The crew](crew.md)) |
| `work-state` | Keeping devkit's records of work in flight true: what each state of an item, run, step, lane, decision, CI watch, and branch means, what to do before ending a turn, after a merge, and before a release, and how to repair drift (see [Work states](work-states.md)) |
| `wait-and-wake` | Waiting without polling: when to arm `dev watch --wait`, the one wait for every devkit event, and how each agent arms it (the Claude Code main session in the background, a crew member or Codex in the foreground), the exit codes, re-arming, and `dev watches` (see [Progress and communication › Waiting and waking](progress-and-communication.md#waiting-and-waking)) |

Slash commands: `/devkit:status`, `/devkit:overview`, `/devkit:task`, `/devkit:handoff`, `/devkit:standup`,
`/devkit:ship`, `/devkit:ci`, `/devkit:dashboard`, `/devkit:doctor`, `/devkit:progress`, `/devkit:crew-status`,
`/devkit:test`, `/devkit:stubs`, and `/devkit:reconcile` (runs `dev task reconcile`, the work state check of
`dev doctor`, `dev ci watches`, and `dev branch prune`, reports the drift, and applies the fixes you agree to). Codex plugins have no slash commands (their manifest takes skills, hooks, apps,
MCP servers, and scheduled tasks), so `dev install` builds Codex's own copy of the plugin in
`~/.local/share/devkit/codex-marketplace/`, where each command is a skill of the same name and content; Codex and the
ChatGPT app list them with the other skills.

The plugin also brings the crew: 25 specialist agents (`devkit:neo`, `devkit:trinity`, `devkit:seraph`, …),
generated from `config/crew.json`, among them the Lookup scout (Sparks), the read-only lookup agent on a small model (`scout` still
works as an alias; see [Models and tokens › The Lookup scout (Sparks)](models-and-tokens.md#the-lookup-scout-sparks)). Codex gets the
same roles in `~/.codex/agents/`. See [The crew](crew.md).

## Required skills

The bundled `configuration` skill is discovered from `plugin/skills` on both Claude Code and Codex.
`config/skill-rules.json` requires it for the configuration files and commands.
`config/skills.json` remains the third-party fetch manifest; bundled skills need no external source entry.

Installing a skill is not enough: the agent has to load it for the work it fits. devkit maps kinds of work to the
skills they require, in `config/skill-rules.json`, and enforces the mapping in Claude Code and Codex alike, except for
one step: only Claude Code's subagent briefs get the required skills added by a hook (see
[Where devkit enforces them](#where-devkit-enforces-them)).

### The rules

| Rule | Claude Code | Codex | Matches (files; commands; kinds of work) |
| --- | --- | --- | --- |
| `ui` | `frontend-design`, `impeccable`, `modern-web-guidance` | the same | HTML, CSS, Sass, Less, TSX, JSX, Vue, and Svelte files; code in other languages, outside tests, that renders HTML or styles or declares an Angular component; `ux`, `frontend`, `design-system`, `accessibility`, and `i18n` work |
| `testing` | `testing` | `testing` | Test files of every stack (C#, Ruby, and Swift test files and `spec/` included), `conftest.py`, test runner configs; `pytest`, `jest`, `vitest`, `dev test`, `python3 -m unittest`; `testing` work |
| `e2e` | `playwright-cli`, `testing` | the same | `playwright.config.*`; spec and test files that import `@playwright/test`; `playwright test`, `playwright-cli` |
| `visual` | `visual-diagnosis` | the same | `ux`, `frontend`, `accessibility`, `design-system` and `mobile` work; `dev visual`, `playwright-cli screenshot` |
| `angular` | `angular-developer` | `angular-developer` | Components, `angular.json`, routes; `ng build`, `ng generate`, `ng serve`, `ng test`, `ng update` |
| `react` | `react-best-practices` | `react-best-practices` | TSX and JSX files |
| `nextjs` | `react-best-practices` | the same | `next.config.*`; TypeScript and JavaScript modules that import `next`; `next build`, `next dev`, `next start`, `next upgrade`. Its reason tells the agent to read the docs Next.js bundles in `node_modules/next/dist/docs/` |
| `vue` | `vue-best-practices` | the same | Vue files, `nuxt.config.*`; TypeScript and JavaScript modules that import Vue, Pinia, Vue Router, or Nuxt |
| `svelte` | `svelte-core-bestpractices` | the same | Svelte files, `.svelte.ts` and `.svelte.js` modules, `svelte.config.*`; modules that import Svelte, SvelteKit, or `$app/` |
| `spring` | `java-springboot`, `backend` | the same | Java and Kotlin files that use `org.springframework` |
| `java-tests` | `java-junit`, `spring-boot-testing` | the same | `*Test.java`, `*Tests.java`, `*IT.java` |
| `mobile` | `mobile` | the same | `AndroidManifest.xml`, `Info.plist`, privacy manifests, entitlements; Swift, Kotlin, and Java code that imports an Apple UI framework or Android, and Android app or library build files; `mobile` work |
| `swiftui` | `swiftui-pro` | the same | Swift files that import SwiftUI |
| `android-manifest` | `android-permissions-security` | the same | `AndroidManifest.xml` |
| `api` | `backend` | `backend` | OpenAPI, GraphQL, and protobuf files; Java, PHP, TypeScript, and Ruby controllers and Rails' `config/routes.rb`; Python, PHP, Node, Go, C#, Rust, and Ruby code, outside tests, that declares HTTP routes or controllers (FastAPI, Flask, Django, Laravel, Symfony, Express, NestJS, Fastify, Hono, `net/http` with Go 1.22 patterns, Gin, Echo, chi, Fiber, ASP.NET Core, axum, actix-web, Rocket, Rails, Sinatra, Grape, Hanami); `backend` and `sso` work |
| `aspnetcore` | `dotnet-webapi` | the same | C# code, outside tests, that maps minimal API endpoints or declares an MVC or API controller (a class that derives from `Controller` or `ControllerBase`, or `[ApiController]`); a class is not a controller by its name alone, so a Unity script or an iOS view controller does not count |
| `dotnet` | `backend` | the same | `*.csproj` and `Directory.Packages.props` of a web or database project (the Web SDK, ASP.NET Core, EF Core, Npgsql, Dapper, gRPC) |
| `rust` | `backend` | the same | `Cargo.toml` that depends on a web, gRPC, or database crate (axum, actix-web, Rocket, Poem, warp, Salvo, tonic, sqlx, Diesel, SeaORM, tokio-postgres) |
| `ruby` | `backend` | the same | `Gemfile` and gemspecs that depend on Rails, Sinatra, Hanami, Grape, Roda, Sequel, Active Record, or a database driver |
| `go` | `backend` | the same | `go.mod` that requires a web, gRPC, or database module (Gin, Echo, chi, Fiber, gorilla/mux, Huma, pgx, `lib/pq`, the MySQL driver, sqlx, Bun, GORM, ent, goose, golang-migrate, gRPC, Connect) |
| `database` | `backend` | `backend` | SQL files and migration folders, Rails' `db/migrate/`, EF Core's `Migrations/`; `alembic`, `flyway`, `liquibase`, `prisma migrate`, `dotnet ef`, `rails db:migrate`, `sqlx migrate`, `diesel migration`, `goose`; `database` work |
| `postgres` | `supabase-postgres-best-practices` | the same | SQL files and migration folders in a repository whose project files name Postgres; `psql`, `pg_dump`, `pg_restore` |
| `workflows` | `cicd`, `github-actions-hardening`, `delivery` | the same | GitHub, GitLab, Bitbucket and Azure pipeline files; `gh workflow`; `cicd` work |
| `security` | `devsecops` | `devsecops` | Dockerfiles, security folders, Terraform; `dev sec scan`; `security`, `sso`, `redteam`, and `dependencies` work |
| `terraform` | `terraform-style-guide` | the same | `*.tf` and `*.tfvars` files, only when the `iac` skill group is installed |
| `docs` | `docs` | `docs` | `README.md`, Markdown in `docs/`, MDX, `CHANGELOG.md`, `CONTRIBUTING.md`, skills (`plugin/skills/**`, any `SKILL.md`), agent files, slash commands, and `agents/AGENTS.md`; `dev docs check`; `docs` work |
| `diagrams` | `diagrams` | `diagrams` | Diagram specs, SVG, C4, and Mermaid files; `dev diagram`; `architecture` work |
| `delivery` | `delivery` | `delivery` | `git push`, `dev push`, `dev pr`, `gh pr create`; `release` work |
| `release` | `delivery`, `release-policy` | the same | `VERSION`, `cliff.toml`, release-please files; `git tag`, `dev release`, `gh release`; `release` work |
| `crew` | `crew`, `working-efficiently` | the same | `plan` work |
| `communicating` | `communicating` | `communicating` | `dev task delegate`, `dev report`; `plan` and `scope` work |
| `learning` | `learning-loop` | `learning-loop` | `dev lesson add`, `dev lesson recur`, `dev lesson prevent`, `git revert`; `incident` work |
| `root-cause` | `root-cause` | the same | `debug` work |
| `research` | `research` | the same | `research` and `architecture` work |
| `architecture` | `architecture`, `system-design` | the same | `architecture` work |
| `incident` | `incident-response`, `root-cause` | the same | `incident` work |
| `performance` | `root-cause` | the same | `performance` work |
| `functional-analysis` | `functional-analysis` | the same | `requirements` work |
| `work-state` | `work-state` | `work-state` | `dev task reconcile`, `dev branch prune`, `dev crew withdraw`, `dev progress confirm`; `release` and `plan` work |
| `configuration` | `configuration` | `configuration` | Devkit schema/core and configuration tests; `dev config set`, `dev config edit`, `dev config validate` |

A rule matches on what the work does, never on the words of your request, which can be in any language: globs on the
files a turn changed (`files`); a regular expression on the content of changed files (`content`, applied to the
`files` matches, or to the `content_files` globs except those in `content_exclude`, so a Python file that renders
HTML counts as UI and a test that holds HTML does not); the command words an agent runs (`commands`, read without
their quoted text and heredocs, with `bin/dev` counted as `dev`); and the kinds of work a step or brief declares
(`activities`, ids from `dev crew raci`). A `project` condition limits a rule to repositories whose project files
match, such as the `postgres` rule, and a `group` condition applies a rule only when its optional skill group is in
`skills.groups`, such as the `terraform` rule. A file on disk outside every repository is scratch work and matches no
rule, and the UI rule's content check also skips `scratchpad/**` and `.tmp/**`, so a probe in a test repository built
there does not count. The UI rule matches `className=` and the styled-components form (`styled.<tag>` followed by a
template, an object, or `.attrs`) as written, so JUnit's `classname=` or a Python list named `styled` is not UI; HTML
tags match in any case.

### Where devkit enforces them

| When | Hook | Claude Code | Codex |
| --- | --- | --- | --- |
| A session starts | SessionStart | The context carries the catalog: the skills of each kind of work, for the agent to pick | The same |
| A crew member is briefed | `dev crew brief` | The brief lists first the skills the crew gates check before the member stops (each procedure phase's, and those the files of its scope require), then the other skills of its role or kind of work; it never calls a gated skill optional | The same |
| A subagent starts | PreToolUse | On the Agent and Task tools, a "devkit · required skills" section is appended to the subagent's brief, with the skills of its declared kind of work (else its member's Responsible one) and of the files it owns | Not enforced: a hook cannot rewrite a `spawn_agent` call, so a member gets its skills only from the brief `dev crew brief` writes |
| A crew member stops | SubagentStop | The crew gates send the member back once if it did not load the skills of its procedure or of the files it changed | The same, judged against the skills installed for Codex |
| The agent finishes a turn | Stop | If the turn changed matching files or ran matching commands without loading a required skill, it goes back once per skill per session to load the skill and review the work; the request names the reason and the files or commands | The same |

devkit reads which skills a session loaded from its transcript, that is, Claude's Skill tool calls and reads of a `SKILL.md`,
and Codex's shell commands that read a `SKILL.md`. Only skills that are installed for that agent are required.

The developer's explicit request still wins. Told explicitly "do not load any skills," a real Claude session was sent
back by the Stop hook with the reason, and the model followed the developer's instruction, which is the intended
precedence.

### Configure the rules

| Key | Effect |
| --- | --- |
| `skills.enforce` | `false` turns off the brief and Stop enforcement points; the crew gates follow `crew.gates.enforce` |
| `skills.rules` | Your rules: one with an existing `id` changes the fields you give, a new `id` adds a rule |

The format and an example are in [Configuration › Skills](configuration.md#skills). `dev doctor` checks core skills
and selected optional groups for each selected agent. Intentionally omitting an optional group does not make the
installation unhealthy.

Work rules still apply to the task. For example, omitting `postgres` does not waive the skill required for a
PostgreSQL schema change: the matching rule reports the missing skill before that work can proceed. Add the group
to `skills.groups` and run `dev install --only skills` when you need that capability.

## Claude Code plugins

These plugins come from the `claude-plugins-official` marketplace and apply when Claude is selected. Language-server
plugins follow the effective language stacks, including runtimes required by a selected framework.

| Plugin | Source · license | Purpose | Why |
| --- | --- | --- | --- |
| `superpowers` | obra/superpowers · MIT | Brainstorming, plans, subagent-driven development, TDD, systematic debugging, verification before completion, review requests | One process backbone, used in both agents |
| `frontend-design` | Anthropic · Apache-2.0 | Distinctive, production-grade UI | The design direction; Codex gets the same skill from devkit (see [Shared skills](#shared-skills-both-agents)) |
| `commit-commands` | Anthropic · Apache-2.0 | `/commit`, `/commit-push-pr`, `/clean_gone` | Low cost; a consistent commit flow |
| `security-guidance` | Anthropic · Apache-2.0 | Pattern-based security warnings on edits | Cheap, useful warnings; its costly reviews are off (see the following sections) |
| `claude-security` | Anthropic | Deep vulnerability scans of the repository, a branch, a PR or a commit, with every finding challenged before it is reported, and verified patch files you apply when you choose | On-demand security review for auth, input handling, and secrets |
| `claude-md-management` | Anthropic | Audit and improve `CLAUDE.md` files and capture session learnings | Keeps repository instructions short and current |
| `typescript-lsp` | Anthropic · Apache-2.0 | TypeScript and JavaScript code intelligence | Definitions, references, and type errors instead of grepping |
| `pyright-lsp` | Anthropic · Apache-2.0 | Python code intelligence | The same, for Python |
| `jdtls-lsp` | Anthropic · Apache-2.0 | Java code intelligence (Eclipse JDT LS) | The same, for Java; needs a Java 25 runtime |
| `php-lsp` | Anthropic · Apache-2.0 | PHP code intelligence (Intelephense) | The same, for PHP; Intelephense premium features need a license |

The LSP plugins need their language servers, which setup installs for the effective stacks:
`typescript-language-server` and `typescript` for Node, `pyright` for Python, `jdtls` for Java, and `intelephense` for
PHP. An unselected stack's plugin is not an installation requirement. This selection adds no Codex LSP plugin.

## Codex plugins

| Plugin | Source · license | Purpose |
| --- | --- | --- |
| `devkit@firefly-devkit` | this repository | The devkit skills and commands |
| `superpowers@openai-curated-remote` | obra/superpowers · MIT | The same process skills as in Claude Code |
| `impeccable@openai-curated-remote` | pbakaus/impeccable · Apache-2.0 | UI design quality: critique, audit, polish, live iteration; Claude Code gets the same skill from devkit |
| `codex-security@openai-curated-remote` | OpenAI | Security scans, analysis, validation, and investigation of code and diffs |

Codex has no LSP plugin mechanism; its global instructions tell it to run the project's type checker and tests.

## MCP servers

| Server | Claude Code | Codex | Notes |
| --- | --- | --- | --- |
| Chrome DevTools | user scope | yes | `chrome-devtools-mcp@1.12.0` pinned, `--no-usage-statistics --no-performance-crux`; performance, network, and accessibility debugging in a real browser; needs Google Chrome |
| Angular CLI | when Angular applies | when Angular applies | `@angular/cli@22.2.6 mcp`: best practices, documentation search, project tools |

Angular applies when `frameworks` includes `angular`, or when the key is absent and legacy defaults apply. An
explicit empty list does not enable Angular. A selected agent is still required. Chrome DevTools remains browser
tooling. An explicit [extension overlay](configuration.md#extensions) can opt into another capability.

Chrome DevTools starts Google Chrome from `/Applications/Google Chrome.app` (with `--channel`, that channel's
app), and devkit does not install it. Without it, the server starts but its tools fail at their first call.
`dev doctor` flags this in the **Google Chrome for the Chrome DevTools MCP** row, with the fix:
`brew install --cask google-chrome`. A server given its own browser (`--executablePath`) or one to connect
to (`--browserUrl`, `--wsEndpoint`) needs no Chrome app.

Figma is not added: it is already available as a claude.ai connector, and Codex can add `figma@openai-curated` per
project when needed.

## Shared skills (both agents)

The catalog lists available skills, not a requirement to install every framework. With explicit framework choices,
Angular skills apply to Angular, React and composition skills to React or Next.js, and Spring skills to Spring Boot.
JUnit follows Java. Filtering applies per skill, so documentation and security skills from the same source stay
available. An absent framework setting preserves legacy framework skills.

NestJS, Laravel, and Symfony use shared backend/testing guidance; no dedicated skill sources are added for them.
Go, Rust, and Ruby have no official agent skill, so the `backend` skill carries their notes; .NET adds the .NET team's
`dotnet-webapi` for ASP.NET Core endpoints. Next.js gets no skill of its own: Next.js 16.2 and later bundle their
version's docs in `node_modules/next/dist/docs/`, which the Next.js team recommends over skills for framework
knowledge, and the `nextjs` rule says so. The Vue, Svelte, .NET, SwiftUI, and Android skills have no framework or
stack setting yet, so every Mac installs them.
Optional `skills.groups` are independent: `all` selects every optional group, not every framework. Matching work
rules still require the skills needed for the task.

Fetched at install time from upstream at the pinned commits in `config/skills.json`, and linked into
`~/.claude/skills` and `~/.agents/skills`.

Both design skills reach both agents. Each agent's plugin brings one of them, so devkit links the other one only where
the plugin does not: `impeccable` (pbakaus/impeccable, tag `skill-v4.8.0`, Apache-2.0) for Claude Code, and
`frontend-design` (anthropics/claude-plugins-official, Apache-2.0) for Codex. The `ui` rule requires both: frontend-design
for the direction, impeccable for hierarchy, layout, typography, color, and the audit.

The framework, backend, and mobile skills reach both agents, each required by its rule:

| Skill | Source · license | Rule |
| --- | --- | --- |
| `vue-best-practices` | vuejs-ai/skills (a community project that plans to move to the Vue organization) · MIT | `vue` |
| `svelte-core-bestpractices` | sveltejs/ai-tools, from the Svelte team · MIT | `svelte` |
| `dotnet-webapi` | dotnet/skills, from the .NET team · MIT | `aspnetcore` |
| `swiftui-pro` | twostraws/SwiftUI-Agent-Skill, tag `2.0.0` and later fixes · MIT | `swiftui` |
| `android-permissions-security`, `jetpack-compose-performance` | android/skills, from Google, tag `v1.0.23` · Apache-2.0 | `android-manifest`; the Mobile engineer's skills |

All six are Markdown only: they run no code at install, at session start, or when the agent follows them.

`modern-web-guidance` (GoogleChrome/modern-web-guidance, tag `v1.0.1`, Apache-2.0) reaches both agents too, and the
`ui` rule requires it: the Chrome team's guides to current HTML, CSS, and client-side JavaScript, Core Web Vitals,
accessibility, security, and browser support. The agent searches them before it writes a web platform feature. Its
searches run `npx`, with the npm package the Chrome team publishes with SLSA provenance. The upstream skill and its
guides call `npx -y modern-web-guidance@latest`, which runs whatever npm publishes next; devkit's copy runs the version
that `pin_npx` in `config/skills.json` names (`modern-web-guidance@1.0.1`). When the pin no longer matches the
skill's calls, the skills step says so and installs nothing of that skill. The same guides are Markdown files in the skill's `guides/` folder, which an
agent can read without npm.

### impeccable and its engine

impeccable's first setup step is its `context` command, which loads the project's PRODUCT.md, DESIGN.md, and surface
brief and prints what to follow. The skill runs its commands through a launcher, `scripts/impeccable`, a small shell
script that runs a platform binary, the engine, placed beside it. Without one, the launcher would download the engine
at its first run, inside an agent session. devkit fetches the engine at install time instead, and runs it with
`dev impeccable <verb>`:

| Part | Claude Code | Codex |
| --- | --- | --- |
| The skill | `pbakaus/impeccable` at the commit pinned in `config/skills.json` (tag `skill-v4.8.0`), copied to `~/.local/share/devkit/skills/impeccable` and linked as `~/.claude/skills/impeccable` | The `impeccable@openai-curated-remote` plugin (`config/codex/plugins.json`) |
| The engine | `dev install --only skills` fetches it (below) | Comes with the plugin; `dev impeccable` runs devkit's copy, as on Claude Code |
| Use by default | The `ui` rule requires `frontend-design` and `impeccable`; the Frontend engineer, the UX designer, and the Mobile engineer run `dev impeccable context` once at the start of their work | The same rule and the same procedure |

How the skills step installs the engine:

1. It reads the engine version from the pinned skill's `scripts/VERSION` (0.1.18 today) and the SHA-256 of this
   platform's asset, such as `impeccable-darwin-arm64`, from the same commit's `scripts/engine-checksums.txt`. It
   never trusts a digest published beside the binary.
2. It downloads only that asset, over HTTPS, from
   `https://github.com/pbakaus/impeccable/releases/download/engine-v<version>/<asset>`, and checks its SHA-256.
3. It installs the verified binary as `scripts/bin/<os>-<arch>/impeccable` (mode 0755), the file the launcher runs
   first, and keeps a verified copy in `~/.local/share/devkit/engines/impeccable/<version>/`, so the next install
   reuses it without a download. Older versions are removed.
4. It records an `install.engine` row in the audit trail (`dev audit`) and writes what it installed to
   `~/.local/state/devkit/skill-engines.json`. Agents may write neither folder.

A digest mismatch, a failed download, or a file error leaves nothing executable, shows a failed line in the install
output, and records it in the audit trail. The rest of the install goes on: the other skills are still copied and
linked. Fix the cause that the line names, then run `dev install --only skills` again. A file error names its path:
fix that path's owner or permissions, or move away a folder that sits where the engine goes.

Claude Code sessions set `IMPECCABLE_NO_UPDATE_CHECK=1`, so the engine does not check the network for a newer
version (devkit moves impeccable by its pinned commit instead, below), and `IMPECCABLE_NO_TELEMETRY=1`, so it sends
no telemetry.

#### How `dev impeccable` runs the engine

`dev impeccable <verb> [args…]` does the launcher's job from devkit's own paths, without a shell:

1. It takes the engine from devkit's skills folder,
   `~/.local/share/devkit/skills/impeccable/scripts/bin/<os>-<arch>/impeccable`, and refuses a path that goes
   through a symbolic link. It never takes the engine's path from the current folder, an argument, or the
   environment.
2. Before every run, it checks that `~/.local/state/devkit/skill-engines.json` names that file, that the file's
   SHA-256 matches the one recorded there, and that the file did not change between the check and the start.
3. It runs the engine in your current folder, with each word after `dev impeccable` as one argument, as typed: no
   shell reads them. For an agent, that folder must be in a project checkout of the workspace (below). The engine gets `IMPECCABLE_SKILL_DIR` (the skill folder) and `IMPECCABLE_SELF`
   (`dev impeccable`, the name it prints in the commands it suggests), as the launcher sets them, plus
   `IMPECCABLE_NO_UPDATE_CHECK=1` and `IMPECCABLE_NO_TELEMETRY=1`. `IMPECCABLE_CRITIQUE_META`, the scores the
   skill's critique step passes to `critique-storage write` as JSON, passes through as data. Every other
   `IMPECCABLE_*` variable is dropped: `IMPECCABLE_BIN` would pick another engine, and the others move its
   downloads, caches, and plugins.
4. It exits with the engine's exit code. A missing record, a digest that does not match, or an engine path outside
   devkit's skills folder runs nothing, prints why, and exits with `1`. The fix is `dev install --only skills`.

Agents and crew members run impeccable with `dev impeccable`, never by the launcher's path: the launcher runs
whatever engine sits beside the path it was run by, while `dev impeccable` runs only the engine devkit installed and
checked. Every verb runs for whoever calls it, from any folder; devkit no longer keeps a list of the verbs an agent
may run. Some verbs reach the internet, cost money, write outside the project, skip one of the skill's gates, or
change the agents' settings or hooks (`detect` on a remote URL, `generate-image`, `doctor --fix`, `hooks on`): in
guided mode, the global instructions have the agent ask you first, as for any decision about risk.

- **Run it from the project folder.** The engine counts the paths it takes (`--target`, `--file`, and the helpers'
  files) from the folder it runs in, so a `cd` first, or a `..` in a path, points it at another project. In a scratch
  run, `live --target ../other-app/index.html` injected the live script into that file.
- **`detect` loads a URL in Chrome.** A URL anywhere but `localhost` or `127.0.0.1` leaves this Mac.

impeccable offers to write PRODUCT.md and DESIGN.md. devkit's global instructions keep agents from creating them
unless the developer asks (see [Settings that tame the plugins](#settings-that-tame-the-plugins)).

`dev doctor` checks the parts used by each selected agent:

| Row | Passes when | Fix |
| --- | --- | --- |
| Agents · impeccable skill (Claude) | `~/.claude/skills/impeccable` links to devkit's copy | `dev install --only skills` |
| Agents · impeccable engine | The engine for this platform is in place and its SHA-256 matches `engine-checksums.txt` | `dev install --only skills` |
| Codex · impeccable plugin | `codex plugin list` shows `impeccable@openai-curated-remote` installed and enabled | `dev install --only codex` |

To move to a newer impeccable, change its `ref` in `config/skills.json` after review and run
`dev install --only skills`. The step fetches the engine version that commit pins.

| Skill | Source · license | Use |
| --- | --- | --- |
| `grill-with-docs` | mattpocock/skills · MIT | Interview the developer to align before work starts. Your own slash command, `/grill-with-docs`: a model cannot load it |
| `domain-modeling` | mattpocock/skills · MIT | Glossary and domain model, ADRs |
| `codebase-design` | mattpocock/skills · MIT | Deep-module design |
| `improve-codebase-architecture` | mattpocock/skills · MIT | Architecture review and refactoring direction. Your own slash command, `/improve-codebase-architecture`: a model cannot load it |
| `architecture` | anthropics/knowledge-work-plugins · Apache-2.0 | Architecture decisions and trade-offs |
| `system-design` | anthropics/knowledge-work-plugins · Apache-2.0 | System design reviews |
| `incident-response` | anthropics/knowledge-work-plugins · Apache-2.0 | Triage, communication, and blameless postmortems |
| `likec4-dsl` | likec4/likec4 · MIT | C4 architecture diagrams as code |
| `react-best-practices` | vercel-labs/agent-skills · MIT (per SKILL.md) | React and Next.js performance patterns |
| `composition-patterns` | vercel-labs/agent-skills · MIT (per SKILL.md) | React component composition |
| `angular-developer` | angular/skills · MIT (per SKILL.md, Google LLC) | Angular code and architecture: signals, forms, DI, routing, SSR, testing |
| `angular-new-app` | angular/skills · MIT (per SKILL.md, Google LLC) | New Angular applications |
| `java-springboot` | github/awesome-copilot · MIT | Spring Boot code |
| `spring-boot-testing` | github/awesome-copilot · MIT | Spring Boot testing |
| `java-junit` | github/awesome-copilot · MIT | JUnit 5 and 6 testing |
| `spring-security` | github/awesome-copilot · MIT | Spring Security configuration and tests |
| `spring-observability` | github/awesome-copilot · MIT | Actuator, Micrometer, tracing, and structured logs |
| `documentation-writer` | github/awesome-copilot · MIT | Diátaxis documentation |
| `github-actions-hardening` | github/awesome-copilot · MIT | Workflow security: injection, triggers, pinning, permissions |
| `dependabot` | github/awesome-copilot · MIT | Dependabot configuration and dependency updates |
| `threat-model-analyst` | github/awesome-copilot · MIT | STRIDE threat models of a repository |
| `doc-coauthoring` | anthropics/skills | A structured workflow for specs, proposals, and decision documents |
| `semgrep` | trailofbits/skills · CC-BY-SA-4.0 | Semgrep scans with a reviewed plan |
| `fp-check` | trailofbits/skills · CC-BY-SA-4.0 | Verify whether a suspected vulnerability is real |
| `variant-analysis` | trailofbits/skills · CC-BY-SA-4.0 | Find other instances of a bug already found |
| `property-based-testing` | trailofbits/skills · CC-BY-SA-4.0 | Property-based tests across languages |
| `otel-instrumentation` | dash0hq/agent-skills · Apache-2.0 | OpenTelemetry traces, metrics, and logs |
| `otel-semantic-conventions` | dash0hq/agent-skills · Apache-2.0 | OpenTelemetry attribute conventions |

### Optional groups

A source with a `group` is installed only when the group is listed in `skills.groups` (or `all` is). The first three
are on by default. Those defaults apply when `skills.groups` is absent; `[]` keeps every optional group off.
Installation health checks use that same selection. A task can still require a skill from an omitted group (see
[Configure the rules](#configure-the-rules)).

| Group | Default | Skills | Source · license |
| --- | --- | --- | --- |
| `security-deep` | on | `agentic-actions-auditor`, `supply-chain-risk-auditor`, `sarif-parsing`, `sharp-edges` | trailofbits/skills · CC-BY-SA-4.0 |
| `postgres` | on | `supabase-postgres-best-practices` | supabase/agent-skills · MIT |
| `observability` | on | `otel-collector` | dash0hq/agent-skills · Apache-2.0 |
| `codeql` | off | `codeql` (private code needs GitHub Code Security) | trailofbits/skills · CC-BY-SA-4.0 |
| `iac` | off | `terraform-style-guide`, `terraform-test` | hashicorp/agent-skills · MPL-2.0 |
| `load-testing` | off | `k6` | grafana/skills · Apache-2.0 |
| `contract-testing` | off | `pactflow` | pactflow/pactflow-agent-skills · MIT |
| `skill-review` | off | `skill-scanner` | getsentry/skills · Apache-2.0 |

```json
{ "skills": { "groups": ["security-deep", "postgres", "observability", "iac"] } }
```

Then run `dev install --only skills`.

### Licenses and attribution

devkit bundles no third-party code: skills are fetched from upstream and keep their own licenses, recorded per skill in
`.devkit-source`. The Trail of Bits skills are licensed CC-BY-SA-4.0 and used unmodified; `NOTICE` carries their
attribution, with the other sources.

## Settings that tame the plugins

| Setting | Where | Why |
| --- | --- | --- |
| Superpowers specs and plans go to `~/Development/.handoff/plans/<id>/` and are not committed | global instructions | Superpowers otherwise writes and commits `docs/superpowers/…` in the repository |
| Superpowers asks before creating a worktree, and uses `dev wt new` | global instructions | Its worktree step otherwise runs after every design approval |
| Where a Superpowers skill and devkit differ, devkit wins: test scale and selection, finishing a branch (`dev push`, `dev pr create`, `dev pr ready`, `dev pr merge`, `dev wt rm`), the plans folder, and worktrees | global instructions | Superpowers' TDD asks for a test first on every change, and its branch-finishing step merges locally with `git merge`, pushes with a raw `git push`, and removes worktrees with `git worktree remove`, outside devkit's PR flow and its one CI run |
| `SUPERPOWERS_DISABLE_TELEMETRY=1` | agent environments, shell | Turns off its version ping |
| `ENABLE_STOP_REVIEW=0`, `ENABLE_COMMIT_REVIEW=0` | Claude `env` | security-guidance would otherwise run an Opus review after every turn and an agent review on every commit |
| `CLAUDE_SECURITY_SCAN_TIP=off` | Claude `env` | No scan tip in every session; run Claude Security when you want it |
| Scanner telemetry off (Trivy, Semgrep, Syft, Grype) | Claude `env`, `dev sec scan` | No usage data or update checks leave the machine |
| No PRODUCT.md or DESIGN.md unless asked | global instructions | impeccable creates them by default |
| Pinned versions for MCP servers launched with `npx` | `config/*/plugins.json` | No unreviewed code on each launch |
| Output folders of the security tools (`CLAUDE-SECURITY-*/`, `threat-model-*/`) | global git ignore | Reports never get committed by accident |

## Per-project vendor tooling

Some of the best tooling is project-scoped and writes files into the repository, so you enable it per project when
you want it (the devkit `workspace` skill knows these commands):

| Stack | Command |
| --- | --- |
| Laravel | `composer require laravel/boost --dev && php artisan boost:install` (choose Claude Code and Codex) |
| Symfony | `composer require --dev symfony/ai-mate && vendor/bin/mate init` |
| FastAPI | `uvx library-skills` (Codex) and `uvx library-skills --claude` (Claude Code) |
| Next.js 16+ | You add the `next-devtools` MCP server, in your terminal, for every project on this Mac: `dev ext add mcp next-devtools --command "npx -y next-devtools-mcp@<exact version>" --license <its license> --why "…"`. Agents cannot run `claude mcp add` or `codex mcp add`, and a server added that way shows as drift |
| New Angular app | `ng new <name> --ai-config=claude-code,open-ai-codex` |
| Large Java codebases | IntelliJ IDEA → Settings → AI → MCP Server → configure it automatically for Claude Code and Codex |

Review the files they write (AGENTS.md, `.mcp.json`, `.codex/config.toml`) before you commit them.

## Who changes extensions, and how drift shows

Plugins, skills, and MCP servers affect later sessions. `dev ext add`, `remove`, `enable`, `disable`,
`update --apply`, and `ack` run for whoever calls them, you or an agent, and keep their own checks: a pin, a license,
and a reason for every entry, and the source and trust checks below. Adding or removing an extension is a decision
about risk: in guided mode, the global instructions have the agent ask you first.

Extension execution audit records retain the action, entry id, exit status, and command/result SHA-256 digests,
never raw arguments, stdout, or stderr. devkit records vendor versions, commits, and content digests in
`~/.local/state/devkit/ext-lock.json`. Missing trust evidence and host permissions remain separate checks. See
[Configuration › Extensions](configuration.md#extensions).

Whatever changes outside devkit anyway shows as drift: at the session start, in `dev status`, `dev doctor`, and
`dev ext ls`, with one `ext.drift` record in the audit trail per change.

| Drift | What it means | What to do |
| --- | --- | --- |
| `changed` | A vendor plugin's version, commit, or files differ from the lock, or an agent's folder for a devkit skill is not devkit's copy | Review it with `dev ext outdated <name>`; accept it with `dev install`, or `dev ext update <name> --apply` |
| `pin` | A skill or MCP server is installed at another commit or version than its pin | `dev install --only skills,claude,codex` |
| `enabled` | A plugin is on or off in the agent's settings against devkit's | `dev config repair` |
| `required-off` | devkit or a security plugin is not installed, or is off in the agent or in a project's `.claude/settings.json` or `.claude/settings.local.json` (the row names the file) | `dev install` puts a missing one back; `dev config lock` keeps required plugins on whatever a project says |
| `not-in-catalog` | A plugin, MCP server, or skill folder is installed outside devkit's catalog, or a project enables a plugin devkit does not manage | `dev ext add` it, or remove it |
| `retired` | An entry devkit dropped is still installed | `dev install` removes it |

What ships with the Codex app itself is listed with the layer `agent`, without drift, only when something vouches
for it, because agents can write Codex's plugin cache and `config.toml`:

- **An `openai-bundled` plugin** counts on its own when every file in its cache folder is byte-equal to the app
  bundle's file at the same path (`ChatGPT.app/Contents/Resources/plugins/openai-bundled/plugins/<name>`), with no
  link. The app copies a subset of the bundle, and for some plugins it rewrites a file such as `.mcp.json`: those
  count only once `dev install` has recorded them.
- **An MCP server** that runs from the app bundle's `Contents/Resources` (links and `..` resolved), is no
  interpreter, agent, or package runner (`node`, `python`, `sh`, `codex`, `npx`, …), and whose environment loads
  no other code counts only once `dev install` has recorded it. A program in the bundle can still run what its
  arguments name (`rg --pre`, `tectonic -Z shell-escape`), so its place proves nothing. Claude Code's MCP servers
  never count.
- **An `openai-primary-runtime` plugin** is downloaded by the app, so it counts once `dev install` has recorded it.

`dev install` names each of them the first time it records it, so you see what it accepts: a plugin with its
version, an MCP server with a configuration digest. Acceptance records and notices do not copy MCP URLs,
arguments, or passwords. The lock keeps a plugin's version and file digest and an MCP configuration digest;
a change after that shows as `changed`. Required source, pin, account, and managed-state checks remain; an unknown
source or missing trust evidence stays unresolved.

An acceptance never claims a person approved it. devkit keeps the acceptance list and audit evidence:

1. Each first record and each change `dev install` or `dev ext update --apply` accepts writes one `ext.accept`
   record to the audit trail, with the entry, the terminal (`tty`), and the parent processes it ran from, and sends
   a desktop notification. The acceptance list is written first: when it is busy, does not parse, or cannot be
   written (a full disk), nothing is accepted and the change stays as drift.
2. The next session start and `dev status` list them: `extensions accepted since your last session: …`, each entry
   cut to one short line without control, bidirectional, or zero-width characters, or other characters that render
   as nothing (variation selectors, Hangul fillers). In Claude Code, the session start
   shows the list to you as a message, not only to the agent.
3. Acknowledge reviewed acceptances with `dev ext ack`. It clears the list and records `ext.ack`; acknowledgment
   does not satisfy missing trust evidence.
4. If you did not, do not acknowledge it. Remove what it accepted (for a Codex MCP server, `codex mcp remove <name>`
   in your terminal), and read `dev audit --action ext.accept` to see which process accepted it.

**Evidence.** The audit trail records which process accepted each change. An acceptance notification is neither
acknowledgment nor approval.

The acceptance notification is a security notice: `DEVKIT_NO_NOTIFY=1` and `progress.notify: false`, which an agent
can set, do not turn it off. It shows only on macOS, and only while notifications are allowed for DevKit, the
branded native app listed in System Settings › Notifications. An agent can change that
setting (`defaults write com.apple.ncprefs`), so the session-start message and the
audit trail are the lasting record of each acceptance.

devkit reads Codex's MCP servers only in the form `codex mcp add` writes: a `[mcp_servers.<name>]` table and its
sub-tables, one `key = value` per line, with a string, number, `true` or `false`, or a one-line array. A server
written another way (a dotted key such as `env.NODE_OPTIONS`, a quoted key, an inline table, a key at the root or
under `[mcp_servers]`, a quoted name) is never the app's and shows as `not-in-catalog`, naming the line. A line
devkit cannot give to any server (a multi-line string it cannot follow, a header it cannot parse) makes none of
them the app's, and shows as the `(config.toml)` row. With Python 3.11 or later, devkit also reads the file as
TOML, and a server it reads differently counts the same way.

The pins have rules of their own:

- **An MCP server** runs through `npx` or `uvx`, and the first package they run carries the exact version
  (`npx -y pkg@1.2.3`, `uvx pkg==1.2.3`, or `uvx --from pkg==1.2.3 <command>`); a runner option that can add or
  swap a package, and variables that load other code or point at another registry (`NODE_OPTIONS`, `PYTHONPATH`,
  `PYTHONSTARTUP`, `DYLD_*`, `LD_*`, `NPM_CONFIG_*`, `UV_INDEX*`), are refused. Otherwise use an `https` URL.
- **A skill pin** moves only to a commit on its repository's default branch, because GitHub serves a fork's commits
  under the parent's name. `dev ext update` checks it, and changes nothing when the review cannot be read.
- **`dev ext update --apply`** accepts only the version it reviewed; anything else stays drift until you check it.
- **`dev install`** names each change it accepts before it records the lock.
- **Your overlay** cannot add a copy of a devkit default, and a required default stays required.
- **A skill name belongs to one source.** One name is one folder for both agents, so `dev ext add skill` refuses a
  name that a devkit default already uses, for either agent, or that devkit puts in the shared skills folder itself
  (a skill from devkit's own tree, or `playwright-cli`), and two sources in `skills.json` with the same skill name
  make the catalog invalid. Names are compared in any case, as the macOS file system compares them, so
  `Playwright-CLI` is refused too.
- **A skill name is a folder name.** It starts with a letter, digit, or underscore, so `dev ext add` refuses `.`,
  `..`, and hidden names. `skills.json` is not held to that rule, so `dev install` skips a name there that would land
  outside the shared skills folder (`..`, `.`, an empty name, or one with a `/`), deletes nothing, and names it:
  rename the skill in `skills.json`, on a branch of the devkit checkout.

## Left out on purpose

| Item | Reason |
| --- | --- |
| `feature-dev` | Its seven-phase workflow and agents duplicate Superpowers' brainstorm, plan, and review steps |
| `code-review`, `code-simplifier` plugins | Duplicate Claude Code's built-in `/code-review` and `/simplify` (the plugin's command collides by name) |
| The full mattpocock plugin alongside Superpowers | About 27 skills that duplicate Superpowers' TDD, debugging, and review triggers; only four design skills are taken |
| `ralph-loop` | Feeds the prompt again in a loop from a Stop hook: it burns tokens, the opposite of a method |
| `hookify` globally | A Python hook on every tool call and turn; devkit's own hooks and commands cover the rules that matter |
| `serena` plugin | Runs an unpinned `uvx --from git+…` on every launch, writes `.serena/`, overlaps the LSP plugins |
| `security-guidance` with defaults | An Opus review after every turn and on every commit (kept, with those reviews off) |
| `vercel` plugin | Dozens of skills, hooks, default-on telemetry and a write-capable remote MCP; useful only when deploying to Vercel |
| `sonarqube`, `postman` globally | Analysis after every edit, or a large always-on footprint; add them per project if needed |
| Kitchen-sink bundles (`wshobson/agents`, knowledge-work `engineering` and `design` plugins, the full `addyosmani/agent-skills`) | Many overlapping skills and remote connectors |
| `openai/skills` as a primary source | Deprecated in favor of `openai/plugins` |
| Sources without a license (`ast-grep/agent-skill`, `JetBrains/junie-guidelines`, others) | Unclear rights for company work |
| `svelte-code-writer` (sveltejs/ai-tools) | Runs `npx @sveltejs/mcp` with no version, so each call runs whatever npm publishes; waits for a Security engineer's review |
| The Next.js skills (`vercel/next.js/skills`) | Migration workflows for Next.js 16.3 and later that run `npx @next/codemod@latest` or `@canary` and install `agent-browser@latest` globally; the bundled docs cover framework knowledge |
| `csharp-expert` (dotnet/skills) | A router that tells the agent to install more marketplace plugins, outside devkit's pins |
| `samber/cc-skills-golang` | One author's collection that steers to the author's own libraries and pre-approves shell tools in `allowed-tools`; the `backend` skill's Go notes cover the basics |
| `swiftui-expert-skill` (AvdLee/SwiftUI-Agent-Skill) | Ships Python scripts that record and parse Instruments traces; waits for a Security engineer's review |
| android/skills' `adaptive`, `testing-setup`, `edge-to-edge`, and the skills with scripts or binaries | Large migrations or dependency installs rather than everyday guidance; `r8-analyzer`, `play-policy-insights`, and `android-profiler` ship scripts or a prebuilt binary |

### Removed

| Item | Removed in | Reason |
| --- | --- | --- |
| `context7` (the Claude Code plugin and the Codex MCP server) | 26.11.4 | Every query left the machine for a third-party service, and the vendor sources it stood in for are now preferred: the project's installed packages, framework MCP servers such as the Angular CLI, then web search. `dev install` uninstalls it from both agents (audited as `ext.retire`) |

An entry removed from devkit's defaults goes to `retired` in its agent's `plugins.json`, and `dev install` uninstalls
it on every Mac.

## Add or update a skill

1. Check the source: maintainer, license, recent activity, what files it writes, whether it sends data anywhere.
2. Add it to `config/skills.json` under its repository, with the full commit SHA in `ref`, the license, a one-line
   `why`, the folder path and, for an optional skill, a `group`:

   ```json
   { "repo": "owner/repo", "ref": "<40-char sha>", "license": "MIT", "why": "Terraform style", "group": "iac", "skills": { "my-skill": "skills/my-skill" } }
   ```

   To move a pinned skill or MCP server to a newer upstream, run `dev ext outdated <name>`, then
   `dev ext update <name> --why "…"` on a branch of the devkit checkout: it prints the compare link, the commits, and
   the files changed before it changes only that pin.

   If the skill runs an npm package as `<package>@latest` (`npx -y <package>@latest …`), pin that package too with
   `pin_npx`, an exact version per package: `"pin_npx": { "modern-web-guidance": "1.0.1" }`. The skills step
   rewrites each `<package>@latest` in the installed copy's text files to that version; a scoped or longer package
   whose name ends with it (`@acme/<package>`) is another package and stays as it is. The step fails and installs
   nothing of that source's skill when the pin is not an exact version, when the skill no longer runs
   `<package>@latest`, or when a call of the package still names no version or another one (`npx <package> …`,
   `<package>@next`), since that copy would run unpinned. When you move `ref`, check the package's
   version that the new commit expects and move the pin with it.

3. If a kind of work should require it, add or change a rule in `config/skill-rules.json`. A skill that sets
   `disable-model-invocation` goes in its source's `user_only` list instead: the developer runs it as a slash
   command, and no crew procedure or rule may ask a model to load it.
4. Run `dev install --only skills`, then `dev doctor`.
5. Record the change in `CHANGELOG.md` and in `NOTICE`.

A devkit skill is a folder with a `SKILL.md` (frontmatter `name` and `description`) in `plugin/skills/`.

See also [Agents](agents.md), [Configuration](configuration.md), and [Models and tokens](models-and-tokens.md).
