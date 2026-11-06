# Skills and plugins

devkit installs a **curated** set, not everything available. Every enabled plugin, skill and MCP server costs context
in each session, and overlapping packs compete to trigger. Both agents already ship code review, simplification,
security review, plan mode and worktrees, so devkit does not duplicate them.

Selection criteria: maintained and widely used, official vendor source where one exists, clear licence, no
uncontrolled telemetry or unpinned downloads, and no behaviour that conflicts with the workspace rules.

## Claude Code plugins

All from the `claude-plugins-official` marketplace, enabled for every project.

| Plugin | Source · licence | Purpose | Why |
|---|---|---|---|
| `superpowers` | obra/superpowers · MIT | Brainstorming, plans, subagent-driven development, TDD, systematic debugging, verification before completion, code review requests | One process backbone, used in both agents |
| `context7` | Upstash · MIT | Up-to-date library documentation (remote MCP) | Version-specific docs when no vendor source exists |
| `frontend-design` | Anthropic · Apache-2.0 | Distinctive, production-grade UI | UI quality in Claude Code (Codex uses impeccable) |
| `commit-commands` | Anthropic · Apache-2.0 | `/commit`, `/commit-push-pr`, `/clean_gone` | Low cost; consistent commit flow |
| `security-guidance` | Anthropic · Apache-2.0 | Pattern-based security warnings on edits | Cheap, useful warnings; its costly reviews are switched off (below) |
| `typescript-lsp` | Anthropic · Apache-2.0 | TypeScript/JavaScript code intelligence | Definitions, references, type errors instead of grepping |
| `pyright-lsp` | Anthropic · Apache-2.0 | Python code intelligence | Same, for Python |
| `jdtls-lsp` | Anthropic · Apache-2.0 | Java code intelligence (Eclipse JDT LS) | Same, for Java; needs a Java 25 runtime |
| `php-lsp` | Anthropic · Apache-2.0 | PHP code intelligence (Intelephense) | Same, for PHP; Intelephense premium features need a licence |

The LSP plugins need their language servers, which `install.sh` provides: `typescript-language-server` and
`typescript`, `pyright`, `jdtls`, `intelephense`.

## Codex plugins

| Plugin | Source · licence | Purpose |
|---|---|---|
| `superpowers@openai-curated-remote` | obra/superpowers · MIT | The same process skills as in Claude Code |
| `impeccable@openai-curated-remote` | pbakaus/impeccable · Apache-2.0 | UI design quality: critique, audit, polish, live iteration |

Codex has no LSP plugin mechanism; its global instructions tell it to run the project's type checker and tests.

## MCP servers

| Server | Claude Code | Codex | Notes |
|---|---|---|---|
| context7 | via the plugin | `https://mcp.context7.com/mcp` | Queries leave the machine |
| Chrome DevTools | user scope | yes | `chrome-devtools-mcp@1.12.0` pinned, `--no-usage-statistics --no-performance-crux`; performance, network, accessibility debugging in a real browser |
| Angular CLI | user scope | yes | `@angular/cli@22.2.6 mcp`: best practices, documentation search, project tools |

Figma is not added: it is already available as a claude.ai connector, and Codex can add `figma@openai-curated` per
project when needed.

## Shared skills (both agents)

Fetched at install time from upstream at the pinned commits in `config/skills.json` and linked into
`~/.claude/skills` and `~/.agents/skills`.

| Skill | Source · licence | Use |
|---|---|---|
| `grill-with-docs` | mattpocock/skills · MIT | Interview the user to align before work starts |
| `domain-modeling` | mattpocock/skills · MIT | Glossary and domain model, ADRs |
| `codebase-design` | mattpocock/skills · MIT | Deep-module design |
| `improve-codebase-architecture` | mattpocock/skills · MIT | Architecture review and refactoring direction |
| `architecture` | anthropics/knowledge-work-plugins · Apache-2.0 | Architecture decisions and trade-offs |
| `system-design` | anthropics/knowledge-work-plugins · Apache-2.0 | System design reviews |
| `likec4-dsl` | likec4/likec4 · MIT | C4 architecture diagrams as code |
| `react-best-practices` | vercel-labs/agent-skills · MIT (per SKILL.md) | React and Next.js performance patterns |
| `composition-patterns` | vercel-labs/agent-skills · MIT (per SKILL.md) | React component composition |
| `angular-developer` | angular/skills · MIT (per SKILL.md, Google LLC) | Angular code and architecture: signals, forms, DI, routing, SSR, testing |
| `angular-new-app` | angular/skills · MIT (per SKILL.md, Google LLC) | New Angular applications |
| `java-springboot` | github/awesome-copilot · MIT | Spring Boot practices |
| `spring-boot-testing` | github/awesome-copilot · MIT | Spring Boot testing |
| `java-junit` | github/awesome-copilot · MIT | JUnit 5 testing |

devkit's own skills, in `agents/skills/`:

| Skill | Use |
|---|---|
| `devkit:handoff` | Update the project's handoff note before finishing or on "handoff / pause / wrap up" |
| `devkit:workspace` | Layout, `dev` commands, worktrees, disk, Docker, per-project vendor tooling |
| `devkit:delivery` | Local-first GitOps: integration branch, one push, one CI run, CI failure handling, releases |
| `devkit:backend` | Backend checklists: API contracts, layering, migrations, testing, resilience, security, operability |

## Settings that tame the plugins

| Setting | Where | Why |
|---|---|---|
| Superpowers specs and plans go to `~/Development/.handoff/plans/<id>/` and are not committed | global instructions | Superpowers otherwise writes and commits `docs/superpowers/…` into the repository |
| Superpowers asks before creating a worktree, and uses `dev wt new` | global instructions | Its worktree step otherwise fires after every design approval |
| `SUPERPOWERS_DISABLE_TELEMETRY=1` | agent environments, shell | Disables its version ping |
| `ENABLE_STOP_REVIEW=0`, `ENABLE_COMMIT_REVIEW=0` | Claude `env` | security-guidance would otherwise run an Opus review after every turn and an agent review on every commit |
| No PRODUCT.md / DESIGN.md unless asked | global instructions | impeccable creates them by default |
| Pinned versions for MCP servers launched with `npx` | `config/*/plugins.json` | No unreviewed code on each launch |

## Per-project vendor tooling

Some of the best tooling is project-scoped and writes files into the repository, so it is enabled per project when
the user wants it (the devkit `workspace` skill knows these commands):

| Stack | Command |
|---|---|
| Laravel | `composer require laravel/boost --dev && php artisan boost:install` (choose Claude Code and Codex) |
| Symfony | `composer require --dev symfony/ai-mate && vendor/bin/mate init` |
| FastAPI | `uvx library-skills` (Codex) and `uvx library-skills --claude` (Claude Code) |
| Next.js 16+ | `claude mcp add -s local next-devtools -- npx next-devtools-mcp@latest` / `codex mcp add next-devtools -- npx next-devtools-mcp@latest` |
| New Angular app | `ng new <name> --ai-config=claude-code,open-ai-codex` |
| Large Java codebases | IntelliJ IDEA → Settings → AI → MCP Server → auto-configure for Claude Code and Codex |

Review the files they write (AGENTS.md, `.mcp.json`, `.codex/config.toml`) before committing.

## Left out on purpose

| Item | Reason |
|---|---|
| `feature-dev` | Its seven-phase workflow and agents duplicate Superpowers' brainstorm, plan and review steps |
| `code-review`, `code-simplifier` plugins | Duplicate Claude Code's built-in `/code-review` and `/simplify` (the plugin's command collides by name) |
| The full mattpocock plugin alongside Superpowers | About 27 skills that duplicate Superpowers' TDD, debugging and review triggers; only four design skills are taken |
| `ralph-loop` | Re-feeds the prompt in a loop from a Stop hook: burns tokens, the opposite of a method |
| `hookify` globally | A Python hook on every tool call and turn; devkit's guard covers the rules that matter |
| `serena` plugin | Runs an unpinned `uvx --from git+…` on every launch, writes `.serena/`, overlaps the LSP plugins |
| `security-guidance` with defaults | An Opus review after every turn and on every commit (kept, with those reviews off) |
| `vercel` plugin | Dozens of skills, hooks, default-on telemetry and a write-capable remote MCP; only useful when deploying to Vercel |
| `modern-web-guidance` | Marks itself "MANDATORY: execute first" for all HTML/CSS/JS work and runs an unpinned `npx …@latest` |
| `sonarqube`, `postman` globally | Analysis after every edit / large always-on footprint; add per project if needed |
| Kitchen-sink bundles (`wshobson/agents`, knowledge-work `engineering`/`design` plugins, full `addyosmani/agent-skills`) | Many overlapping skills and remote connectors |
| `openai/skills` as a primary source | Deprecated in favour of `openai/plugins` |
| Sources without a licence (`ast-grep/agent-skill`, `JetBrains/junie-guidelines`, others) | Unclear rights for company work |

## Adding or bumping a skill

1. Check the source: maintainer, licence, recent activity, what files it writes, whether it phones home.
2. Add it to `config/skills.json` under its repository with the full commit SHA in `ref` and the folder path:

   ```json
   { "repo": "owner/repo", "ref": "<40-char sha>", "license": "MIT", "skills": { "my-skill": "skills/my-skill" } }
   ```

3. `dev install --only skills`, then `dev doctor`.
4. Record the change in `CHANGELOG.md` and in `NOTICE`.

A devkit skill is a folder with a `SKILL.md` (front matter `name` and `description`) in `agents/skills/`.

See also: [Agents](agents.md) · [Configuration](configuration.md)
