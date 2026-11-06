# Skills and plugins

This page lists what devkit installs into Claude Code and Codex, why each item is there, how devkit makes sure the
right skills are used for each kind of work, and what was left out on purpose.

devkit installs a **curated** set, not everything available. Every enabled plugin, skill, and MCP server costs context
in each session, and overlapping packs compete to trigger. Both agents already ship code review, simplification,
security review, plan mode and worktrees, so devkit does not duplicate them.

Selection criteria: maintained and widely used, an official vendor source where one exists, a clear license, no
uncontrolled telemetry or unpinned downloads, and no behavior that conflicts with the workspace rules.

## The devkit plugin

devkit ships its own plugin, `devkit@firefly-devkit`, for both agents. `dev plugin validate` checks it with Claude
Code's validator and Codex's own parser, and `dev install` reinstalls an agent's copy when it differs from the
checkout.

| Skill | Use |
| --- | --- |
| `using-devkit` | The operating manual: the session protocol, which `dev` command and skill fits each job, projects versus initiatives, the status block |
| `handoff` | Update the project's handoff note before finishing, or when you say `handoff`, `pause`, or `wrap up` |
| `communicating` | Templates for every message: kickoff, progress, status, decision, explanation, final report, failure, PR description, merge message, review comments, delegation, and the structured formats (see [Progress and communication](progress-and-communication.md#how-agents-write-to-you)) |
| `working-efficiently` | Models, effort, subagents, and token hygiene (see [Models and tokens](models-and-tokens.md)) |
| `workspace` | Layout, `dev` commands, worktrees, disk, Docker, per-project vendor tooling |
| `delivery` | Local-first GitOps: integration branch, one push, one CI run, CI failures, releases |
| `backend` | API contracts, layering, migrations, testing, resilience, security, operability |
| `devsecops` | Threat modeling, secrets, dependencies and supply chain, SAST, CI hardening, containers and infrastructure, and `dev sec scan` |
| `docs` | Documentation to the devkit standard: Diátaxis, Google style, American English, `dev docs check` |
| `diagrams` | Branded, validated SVG diagrams with `dev diagram` |

Slash commands (in Codex, skills of the same name): `/devkit:status`, `/devkit:overview`, `/devkit:task`,
`/devkit:handoff`, `/devkit:standup`, `/devkit:ship`, `/devkit:ci`, `/devkit:dashboard`, and `/devkit:doctor`.

The plugin also brings the `scout` agent for Claude Code: a read-only lookup agent on a small model (see
[Models and tokens › The scout agent](models-and-tokens.md#the-scout-agent)). Codex gets the same role from
`~/.codex/agents/scout.toml`.

## Required skills

Installing a skill is not enough: the agent has to load it for the work it fits. devkit maps kinds of work to the
skills they require, in `config/skill-rules.json`, and enforces the mapping in both agents.

### The rules

| Rule | Claude Code | Codex | Matches (files, or a request about…) |
| --- | --- | --- | --- |
| `ui` | `frontend-design` | `impeccable` | HTML, CSS, Sass, Less, TSX, JSX, Vue, and Svelte files, code in other languages that renders HTML or styles; UI, UX, frontend, dashboards, layout, screens |
| `angular` | `angular-developer` | `angular-developer` | Components, `angular.json`, routes; Angular |
| `react` | `react-best-practices` | `react-best-practices` | TSX and JSX files; React, Next.js |
| `spring` | `java-springboot`, `backend` | the same | Java and Kotlin files that use `org.springframework`; Spring, Java |
| `java-tests` | `java-junit`, `spring-boot-testing` | the same | `*Test.java`, `*Tests.java`, `*IT.java` |
| `api` | `backend` | `backend` | OpenAPI, GraphQL, and protobuf files, controllers; APIs, endpoints, backends |
| `database` | `supabase-postgres-best-practices`, `backend` | the same | SQL files and migration folders; migrations, schemas, Postgres, indexes |
| `workflows` | `github-actions-hardening`, `delivery` | the same | `.github/workflows/` and `.github/actions/`; GitHub Actions, CI pipelines |
| `security` | `devsecops` | `devsecops` | Dockerfiles, security folders, Terraform; security, auth, secrets, vulnerabilities |
| `docs` | `docs` | `docs` | `README.md`, `docs/**/*.md`, MDX, `CHANGELOG.md`, `CONTRIBUTING.md`; documentation, guides |
| `diagrams` | `diagrams` | `diagrams` | Diagram specs, SVG, C4, and Mermaid files; diagrams |
| `delivery` | `delivery` | `delivery` | A request about releases, deployments, pushes, merges, or pull requests |
| `communicating` | `communicating` | `communicating` | A request about status reports, delegation, or handovers |

Each rule matches on any of three things: globs on the files a turn changed (`files`); a regular expression on the
content of changed files (`content`, applied to the `files` matches, or to the `content_files` globs, so a Python file
that renders HTML counts as UI); and a regular expression on your request or on a subagent's brief (`prompt`). The
prompt patterns also understand common Spanish words.

### Where devkit enforces them

| When | Hook | What happens |
| --- | --- | --- |
| You send a request | UserPromptSubmit | The agent is told which skills the request needs that it has not loaded yet, and why |
| Claude starts a subagent | PreToolUse on the Agent and Task tools | A "devkit · required skills" section is appended to the subagent's brief, so the subagent loads them too |
| The agent finishes a turn | Stop | If the turn changed matching files without loading a required skill, the turn goes back once per skill per session, with the reason and the files, to load the skill, and review the change |

devkit reads which skills a session loaded from its transcript, that is, Claude's Skill tool calls and reads of a `SKILL.md`,
and Codex's shell commands that read a `SKILL.md`. Only skills that are installed for that agent are required.

The developer's explicit request still wins. Two real Claude sessions show both sides:

- Asked for `a simple, accessible base stylesheet`, the session received the skill reminder, loaded
  `frontend-design` with the Skill tool before writing, and needed no second pass.
- Told explicitly "do not load any skills," the session was sent back by the Stop hook with the reason, and the model
  followed the developer's instruction, which is the intended precedence.

### Configure the rules

| Key | Effect |
| --- | --- |
| `skills.enforce` | `false` turns all three enforcement points off |
| `skills.rules` | Your rules: one with an existing `id` changes the fields you give, a new `id` adds a rule |

The format and an example are in [Configuration › Skills](configuration.md#skills). `dev doctor` checks, for each
agent, that every rule's skills are installed ("skills for every rule"); removing the `postgres` group, for example,
leaves the `database` rule without its skill.

## Claude Code plugins

From the `claude-plugins-official` marketplace, enabled for every project.

| Plugin | Source · license | Purpose | Why |
| --- | --- | --- | --- |
| `superpowers` | obra/superpowers · MIT | Brainstorming, plans, subagent-driven development, TDD, systematic debugging, verification before completion, review requests | One process backbone, used in both agents |
| `context7` | Upstash · MIT | Up-to-date library documentation (remote MCP) | Version-specific docs when no vendor source exists |
| `frontend-design` | Anthropic · Apache-2.0 | Distinctive, production-grade UI | UI quality in Claude Code (Codex uses impeccable) |
| `commit-commands` | Anthropic · Apache-2.0 | `/commit`, `/commit-push-pr`, `/clean_gone` | Low cost; a consistent commit flow |
| `security-guidance` | Anthropic · Apache-2.0 | Pattern-based security warnings on edits | Cheap, useful warnings; its costly reviews are off (see the following sections) |
| `claude-security` | Anthropic | Deep vulnerability scans of the repository, a branch, a PR or a commit, with every finding challenged before it is reported, and verified patch files you apply when you choose | On-demand security review for auth, input handling, and secrets |
| `claude-md-management` | Anthropic | Audit and improve `CLAUDE.md` files and capture session learnings | Keeps repository instructions short and current |
| `typescript-lsp` | Anthropic · Apache-2.0 | TypeScript and JavaScript code intelligence | Definitions, references, and type errors instead of grepping |
| `pyright-lsp` | Anthropic · Apache-2.0 | Python code intelligence | The same, for Python |
| `jdtls-lsp` | Anthropic · Apache-2.0 | Java code intelligence (Eclipse JDT LS) | The same, for Java; needs a Java 25 runtime |
| `php-lsp` | Anthropic · Apache-2.0 | PHP code intelligence (Intelephense) | The same, for PHP; Intelephense premium features need a license |

The LSP plugins need their language servers, which `dev setup` installs for your stacks: `typescript-language-server`
and `typescript`, `pyright`, `jdtls`, and `intelephense`.

## Codex plugins

| Plugin | Source · license | Purpose |
| --- | --- | --- |
| `devkit@firefly-devkit` | this repository | The devkit skills and commands |
| `superpowers@openai-curated-remote` | obra/superpowers · MIT | The same process skills as in Claude Code |
| `impeccable@openai-curated-remote` | pbakaus/impeccable · Apache-2.0 | UI design quality: critique, audit, polish, live iteration |
| `codex-security@openai-curated-remote` | OpenAI | Security scans, analysis, validation, and investigation of code and diffs |

Codex has no LSP plugin mechanism; its global instructions tell it to run the project's type checker and tests.

## MCP servers

| Server | Claude Code | Codex | Notes |
| --- | --- | --- | --- |
| context7 | through the plugin | `https://mcp.context7.com/mcp` | Queries leave the machine |
| Chrome DevTools | user scope | yes | `chrome-devtools-mcp@1.12.0` pinned, `--no-usage-statistics --no-performance-crux`; performance, network, and accessibility debugging in a real browser |
| Angular CLI | user scope | yes | `@angular/cli@22.2.6 mcp`: best practices, documentation search, project tools |

Figma is not added: it is already available as a claude.ai connector, and Codex can add `figma@openai-curated` per
project when needed.

## Shared skills (both agents)

Fetched at install time from upstream at the pinned commits in `config/skills.json`, and linked into
`~/.claude/skills` and `~/.agents/skills`.

| Skill | Source · license | Use |
| --- | --- | --- |
| `grill-with-docs` | mattpocock/skills · MIT | Interview the developer to align before work starts |
| `domain-modeling` | mattpocock/skills · MIT | Glossary and domain model, ADRs |
| `codebase-design` | mattpocock/skills · MIT | Deep-module design |
| `improve-codebase-architecture` | mattpocock/skills · MIT | Architecture review and refactoring direction |
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
are on by default.

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
| Next.js 16+ | `claude mcp add -s local next-devtools -- npx next-devtools-mcp@latest` · `codex mcp add next-devtools -- npx next-devtools-mcp@latest` |
| New Angular app | `ng new <name> --ai-config=claude-code,open-ai-codex` |
| Large Java codebases | IntelliJ IDEA → Settings → AI → MCP Server → configure it automatically for Claude Code and Codex |

Review the files they write (AGENTS.md, `.mcp.json`, `.codex/config.toml`) before you commit them.

## Left out on purpose

| Item | Reason |
| --- | --- |
| `feature-dev` | Its seven-phase workflow and agents duplicate Superpowers' brainstorm, plan, and review steps |
| `code-review`, `code-simplifier` plugins | Duplicate Claude Code's built-in `/code-review` and `/simplify` (the plugin's command collides by name) |
| The full mattpocock plugin alongside Superpowers | About 27 skills that duplicate Superpowers' TDD, debugging, and review triggers; only four design skills are taken |
| `ralph-loop` | Feeds the prompt again in a loop from a Stop hook: it burns tokens, the opposite of a method |
| `hookify` globally | A Python hook on every tool call and turn; devkit's guard covers the rules that matter |
| `serena` plugin | Runs an unpinned `uvx --from git+…` on every launch, writes `.serena/`, overlaps the LSP plugins |
| `security-guidance` with defaults | An Opus review after every turn and on every commit (kept, with those reviews off) |
| `vercel` plugin | Dozens of skills, hooks, default-on telemetry and a write-capable remote MCP; useful only when deploying to Vercel |
| `modern-web-guidance` | Marks itself "MANDATORY: execute first" for all HTML, CSS, and JavaScript work and runs an unpinned `npx …@latest` |
| `sonarqube`, `postman` globally | Analysis after every edit, or a large always-on footprint; add them per project if needed |
| Kitchen-sink bundles (`wshobson/agents`, knowledge-work `engineering` and `design` plugins, the full `addyosmani/agent-skills`) | Many overlapping skills and remote connectors |
| `openai/skills` as a primary source | Deprecated in favor of `openai/plugins` |
| Sources without a license (`ast-grep/agent-skill`, `JetBrains/junie-guidelines`, others) | Unclear rights for company work |

## Add or update a skill

1. Check the source: maintainer, license, recent activity, what files it writes, whether it sends data anywhere.
2. Add it to `config/skills.json` under its repository, with the full commit SHA in `ref`, the folder path and,
   for an optional skill, a `group`:

   ```json
   { "repo": "owner/repo", "ref": "<40-char sha>", "license": "MIT", "group": "iac", "skills": { "my-skill": "skills/my-skill" } }
   ```

3. If a kind of work should require it, add or change a rule in `config/skill-rules.json`.
4. Run `dev install --only skills`, then `dev doctor`.
5. Record the change in `CHANGELOG.md` and in `NOTICE`.

A devkit skill is a folder with a `SKILL.md` (frontmatter `name` and `description`) in `plugin/skills/`.

See also [Agents](agents.md), [Configuration](configuration.md), and [Models and tokens](models-and-tokens.md).
