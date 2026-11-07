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
| `handoff` | Update the project's handoff note before finishing, or when you say `handoff`, `pause`, or `wrap up` |
| `communicating` | Templates for every message: kickoff, progress, status, decision, explanation, final report, failure, PR description, merge message, review comments, delegation, and the structured formats (see [Progress and communication](progress-and-communication.md#how-agents-write-to-you)) |
| `working-efficiently` | Models, effort, subagents, and token hygiene (see [Models and tokens](models-and-tokens.md)) |
| `workspace` | Layout, `dev` commands, worktrees, disk, Docker, per-project vendor tooling |
| `delivery` | Local-first GitOps: integration branch, one push, one CI run, CI failures, releases |
| `release-policy` | The delivery policy: detect, recommend, and record versioning, bumps, the test gate, the changelog, and the release flow with `dev delivery`; release trains |
| `learning-loop` | Learn from mistakes: record a blameless lesson after a fix, a revert, a repeated refusal, or a correction; recall the known pitfalls; count repeats; turn recurring lessons into automation (see [Lessons](lessons.md)) |
| `backend` | API contracts, layering, migrations, testing, resilience, security, operability |
| `devsecops` | Threat modeling, secrets, dependencies and supply chain, SAST, CI hardening, containers and infrastructure, and `dev sec scan` |
| `docs` | Documentation to the devkit standard: Diátaxis, Google style, American English, `dev docs check` |
| `diagrams` | Branded, validated SVG diagrams with `dev diagram` |
| `testing` | Tests that can fail for a real bug, a strategy per kind of code, TDD done right, running only what a change can break (`dev test`), flaky tests, mutation testing, diagnosing an existing suite (see [Testing](testing.md)) |
| `cicd` | Fast, reliable, DevSecOps pipelines on GitHub Actions, GitLab, Bitbucket and Azure: `dev ci audit` and `dev ci init` (see [CI/CD pipelines](ci-cd.md)) |
| `crew` | Leading devkit's crew: who to send, parallel or in sequence, briefs, lanes, scopes, messages, delegation, guided or automatic decisions (see [The crew](crew.md)) |

Slash commands: `/devkit:status`, `/devkit:overview`, `/devkit:task`, `/devkit:handoff`, `/devkit:standup`,
`/devkit:ship`, `/devkit:ci`, `/devkit:dashboard`, `/devkit:doctor`, `/devkit:progress`, `/devkit:crew-status`,
`/devkit:test`, and `/devkit:stubs`. Codex plugins have no slash commands (their manifest takes skills, hooks, apps,
MCP servers, and scheduled tasks), so `dev install` builds Codex's own copy of the plugin in
`~/.local/share/devkit/codex-marketplace/`, where each command is a skill of the same name and content; Codex and the
ChatGPT app list them with the other skills.

The plugin also brings the crew: 25 specialist agents (`devkit:neo`, `devkit:trinity`, `devkit:seraph`, …),
generated from `config/crew.json`, among them Sparks, the read-only lookup agent on a small model (`scout` still
works as an alias; see [Models and tokens › Sparks, the scout agent](models-and-tokens.md#sparks-the-scout-agent)). Codex gets the
same roles in `~/.codex/agents/`. See [The crew](crew.md).

## Required skills

Installing a skill is not enough: the agent has to load it for the work it fits. devkit maps kinds of work to the
skills they require, in `config/skill-rules.json`, and enforces the mapping in both agents.

### The rules

| Rule | Claude Code | Codex | Matches (files; commands; kinds of work) |
| --- | --- | --- | --- |
| `ui` | `frontend-design`, `impeccable` | the same | HTML, CSS, Sass, Less, TSX, JSX, Vue, and Svelte files, code in other languages that renders HTML or styles; `ux`, `frontend`, `design-system`, `accessibility`, and `i18n` work |
| `testing` | `testing` | `testing` | Test files of every stack, `conftest.py`, test runner configs; `pytest`, `jest`, `vitest`, `dev test`, `python3 -m unittest`; `testing` work |
| `angular` | `angular-developer` | `angular-developer` | Components, `angular.json`, routes; `ng build`, `ng generate`, `ng serve`, `ng test`, `ng update` |
| `react` | `react-best-practices` | `react-best-practices` | TSX and JSX files; `next build`, `next dev` |
| `spring` | `java-springboot`, `backend` | the same | Java and Kotlin files that use `org.springframework` |
| `java-tests` | `java-junit`, `spring-boot-testing` | the same | `*Test.java`, `*Tests.java`, `*IT.java` |
| `api` | `backend` | `backend` | OpenAPI, GraphQL, and protobuf files, controllers; `backend` and `sso` work |
| `database` | `supabase-postgres-best-practices`, `backend` | the same | SQL files and migration folders; `alembic`, `flyway`, `liquibase`, `psql`, `prisma migrate`; `database` work |
| `workflows` | `cicd`, `github-actions-hardening`, `delivery` | the same | GitHub, GitLab, Bitbucket and Azure pipeline files; `gh workflow`, `gh run`; `cicd` work |
| `security` | `devsecops` | `devsecops` | Dockerfiles, security folders, Terraform; `dev sec scan`; `security`, `sso`, `redteam`, and `dependencies` work |
| `docs` | `docs` | `docs` | `README.md`, `docs/**/*.md`, MDX, `CHANGELOG.md`, `CONTRIBUTING.md`; `dev docs check`; `docs` work |
| `diagrams` | `diagrams` | `diagrams` | Diagram specs, SVG, C4, and Mermaid files; `dev diagram`; `architecture` work |
| `delivery` | `delivery`, `release-policy` | the same | `CHANGELOG.md`, `VERSION`, `cliff.toml`, release-please files; `git push`, `git tag`, `dev push`, `dev release`, `dev pr`, `gh pr create`, `gh release`; `release` work |
| `crew` | `crew`, `working-efficiently` | the same | `plan` work |
| `communicating` | `communicating` | `communicating` | `dev task delegate`, `dev report`; `plan` and `scope` work |
| `learning` | `learning-loop` | `learning-loop` | `dev lesson`, `git revert`; `incident` work |

A rule matches on what the work does, never on the words of your request, which can be in any language: globs on the
files a turn changed (`files`); a regular expression on the content of changed files (`content`, applied to the
`files` matches, or to the `content_files` globs, so a Python file that renders HTML counts as UI); the command words
an agent runs (`commands`); and the kinds of work a step or brief declares (`activities`, ids from `dev crew raci`).

### Where devkit enforces them

| When | Hook | What happens |
| --- | --- | --- |
| Claude starts a subagent | PreToolUse on the Agent and Task tools | A "devkit · required skills" section is appended to the subagent's brief, with the skills of its declared kind of work (else its member's Responsible one) and of the files it owns, so the subagent loads them too |
| A crew member stops | SubagentStop | The crew gates send the member back once if it did not load the skills of its procedure or of the files it changed |
| The agent finishes a turn | Stop | If the turn changed matching files without loading a required skill, the turn goes back once per skill per session, with the reason and the files, to load the skill, and review the change |

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

The format and an example are in [Configuration › Skills](configuration.md#skills). `dev doctor` checks, for each
agent, that every rule's skills are installed ("skills for every rule"); removing the `postgres` group, for example,
leaves the `database` rule without its skill.

## Claude Code plugins

From the `claude-plugins-official` marketplace, enabled for every project.

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

The LSP plugins need their language servers, which `dev setup` installs for your stacks: `typescript-language-server`
and `typescript`, `pyright`, `jdtls`, and `intelephense`.

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
| Chrome DevTools | user scope | yes | `chrome-devtools-mcp@1.12.0` pinned, `--no-usage-statistics --no-performance-crux`; performance, network, and accessibility debugging in a real browser |
| Angular CLI | user scope | yes | `@angular/cli@22.2.6 mcp`: best practices, documentation search, project tools |

Figma is not added: it is already available as a claude.ai connector, and Codex can add `figma@openai-curated` per
project when needed.

## Shared skills (both agents)

Fetched at install time from upstream at the pinned commits in `config/skills.json`, and linked into
`~/.claude/skills` and `~/.agents/skills`.

Both design skills reach both agents. Each agent's plugin brings one of them, so devkit links the other one only where
the plugin does not: `impeccable` (pbakaus/impeccable, tag `skill-v4.8.0`, Apache-2.0) for Claude Code, and
`frontend-design` (anthropics/claude-plugins-official, Apache-2.0) for Codex. The `ui` rule requires both: frontend-design
for the direction, impeccable for hierarchy, layout, typography, color, and the audit.

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
   shell reads them. The engine gets `IMPECCABLE_SKILL_DIR` (the skill folder) and `IMPECCABLE_SELF`
   (`dev impeccable`, the name it prints in the commands it suggests), as the launcher sets them, plus
   `IMPECCABLE_NO_UPDATE_CHECK=1` and `IMPECCABLE_NO_TELEMETRY=1`. Every other `IMPECCABLE_*` variable is dropped:
   `IMPECCABLE_BIN` would pick another engine, and the others move its downloads, caches, and plugins.
4. It exits with the engine's exit code. A missing record, a digest that does not match, or an engine path outside
   devkit's skills folder runs nothing, prints why, and exits with `1`. The fix is `dev install --only skills`.

Crew members on Claude Code and Codex run impeccable only with `dev impeccable`. The [guard](guard.md) trusts no
typed path to the launcher. The shell can reach another file than the one the guard reads by the same words (a
symbolic link, `..` after `cd`, a quoted `~`, a glob, or a folder moved earlier in the same command), and the launcher
names the program it runs only at run time, so the guard refuses a crew lane that types the launcher's path, as it
refuses any such script. With `dev impeccable`, agents get an allowlist of verbs, not a list of forbidden ones: the
engine has verbs its help does not show, and an engine update can add more.

| Verb | Read-only lanes | Write lanes and the main session | What it does |
| --- | --- | --- | --- |
| `context` | yes, with `--target` | yes | Loads PRODUCT.md, DESIGN.md, and the surface brief, and prints what to follow |
| `detect` | yes, on local paths (below) | yes | Scans HTML and CSS for design anti-patterns |
| `doctor` | yes, with `--json` or `--target` | yes, with `--json` or `--target` | Reports drift between the project's impeccable files and the engine; never `--fix`, which moves hook entries |
| `live` | no | yes | Starts the live server and adds its script to the page |
| `generate`, `document`, `extract`, `init` | no | yes | The design commands that write project files. Engine 0.1.18 runs them in the agent's chat, not as engine verbs, so `dev impeccable` reaches them only if a newer engine adds them |

- **Each verb counts only as listed:** the first word, spelled as in the table. A verb after `skills` (the engine's
  legacy namespace) or after a second `--`, or in another case, such as `Context`, is not on the list.
- **Every other verb is the developer's,** in a terminal: `hooks`, `install`, `link`, `update`, `pin`, `unpin`, and
  `doctor --fix` write the agents' settings, hooks, or skill folders, which change what runs in every later session.
  The same goes for the engine's hidden helpers (`hook`, `hook-admin`, `hook-before-edit`, `build-phase`,
  `capture-server`, the `live-…` helpers, and others), `ignore` and `ignores`, `telemetry`, `check`, `help`, a call
  without a verb, and any verb a newer engine adds. The guard refuses them to every agent, in the main session and in every lane, and
  `dev impeccable` itself refuses them outside the developer's terminal.
- **Read-only lanes** run the read verbs with their reading flags: `context` with `--target`, `detect` with `--json`,
  `--quiet`, `--scope`, `--list-rules`, and the `--no-…` filters, and `doctor` with `--json` or `--target`. These
  verbs do not change your sources, but the engine may still write small cache files of its own, such as its
  staleness check.
- **`detect` scans a URL when it gets one,** so in a read-only lane every path and `--target` value must have a local
  path shape: it starts with `./`, `../`, `/`, or `~/` (or is `.` or `..`), or its first part has only letters,
  digits, `_`, and `-` and is not a host on its own (`localhost`, or a number such as `2130706433`). The guard
  refuses anything else: a colon (a scheme or a port, such as `localhost:3000`), a start of `//`, a domain, an
  address (`example.com`, `example.com.`, `10.0.0.2`), `user@example.com`, a query or a fragment (`?`, `#`), and a
  leading space. A file in the project's top folder with a dotted name counts too, so write it as a path:
  `dev impeccable detect ./index.html`.

impeccable offers to write PRODUCT.md and DESIGN.md. devkit's global instructions keep agents from creating them
unless the developer asks (see [Settings that tame the plugins](#settings-that-tame-the-plugins)).

`dev doctor` checks all three parts:

| Row | Passes when | Fix |
| --- | --- | --- |
| Agents · impeccable skill (Claude) | `~/.claude/skills/impeccable` links to devkit's copy | `dev install --only skills` |
| Agents · impeccable engine | The engine for this platform is in place and its SHA-256 matches `engine-checksums.txt` | `dev install --only skills` |
| Codex · impeccable plugin | `codex plugin list` shows `impeccable@openai-curated-remote` installed and enabled | `dev install --only codex` |

To move to a newer impeccable, change its `ref` in `config/skills.json` after review and run
`dev install --only skills`. The step fetches the engine version that commit pins.

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

## Who changes extensions, and how drift shows

Plugins, skills, and MCP servers run in every session, so only you change them, in a terminal, with `dev ext add`,
`remove`, `enable`, `disable`, or `dev ext update <name> --apply`. devkit audits each change and records each vendor
plugin in its lock (`~/.local/state/devkit/ext-lock.json`): the version, the commit, and a digest of the plugin's
files.

The guard refuses the same changes from an agent, through `dev ext` or through the agents' own CLIs. A `claude`
or `codex` call whose words include `plugin`, `plugins`, `mcp`, or `marketplace` anywhere is refused unless the
whole command is one of these read-only forms, optionally with `-h`, `--help`, or `--json`:

- `claude plugin list`, `claude plugin details <name>`, `claude plugin validate <path>`, and
  `claude plugin marketplace list` (`claude plugins` is the same command)
- `claude mcp list` and `claude mcp get <name>`
- `codex plugin list`, `codex plugin marketplace list`, `codex mcp list`, and `codex mcp get <name>`
- a group alone, such as `claude mcp`, which prints its help

Any other form is refused even when it only reads, such as `claude -- plugin list` or `claude --debug plugin list`.
The rule is an allowlist that fails closed: two earlier block lists of verbs each lost to a spelling nobody listed,
such as an option's value read as the group (`claude --model mcp … plugin install x`). The guard finds the program
in the forms it reads: a relative path from the folder the command runs in, a link with another name on your
`PATH`, the npm package or command a runner starts (`npx`, `npm exec`, `bun x`, `yarn exec`, `pnpm claude`), or the
CLI's own script a JS runtime runs. A program it cannot know before the command runs (a variable, a `PATH` the
command sets, a copy the command makes) counts as one of them. With `claude` or `codex`, a word the shell expands
(`$P`, `"$@"`, `$(…)`, `{plugin,install}`, `plugi?`) or a word `xargs` adds counts as a group word, so
`claude -p "$PROMPT"` is refused too. devkit itself also refuses `dev ext update --apply` and
`dev ext enable|disable --project` outside your terminal.

The guard reads the command `screen`, `tmux`, and `expect -c` start, and refuses opening a Terminal or iTerm window
or a `.command` file, whose text it cannot follow. It cannot see everything: a program name built at run time
(`$(printf claude)`, an alias), a script piped into a shell, a command a git alias or `at` starts later, and options
that load a plugin for one session only (`claude --mcp-config`, `--plugin-dir`) get through; [What the guard cannot
see](guard.md#what-the-guard-cannot-see) lists them. Drift and the acceptance list are the backstop for all of these.

Whatever changes outside devkit anyway shows as drift: at the session start, in `dev status`, `dev doctor`, and
`dev ext ls`, with one `ext.drift` record in the audit trail per change.

| Drift | What it means | What to do |
| --- | --- | --- |
| `changed` | A vendor plugin's version, commit, or files differ from the lock, or an agent's folder for a devkit skill is not devkit's copy | Review it with `dev ext outdated <name>`; accept it with `dev install` in your terminal, or `dev ext update <name> --apply` |
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
version, an MCP server with the command and arguments it runs. It keeps a plugin's version and file digest, and an
MCP server's raw text in `config.toml`; a change after that shows as `changed`. Only a `dev install` that devkit
takes for your terminal records them for the first time or accepts a change. One started anywhere else, such as
from an agent's program, only names them, and they stay as drift.

A terminal an agent opens can look like yours: a command it starts in `screen`, `tmux`, or a Terminal window has no
agent marker, a terminal of its own, and launchd as its parent, and no check inside a process can prove that you
typed it. devkit therefore shows every acceptance until you acknowledge it:

1. Each first record and each change `dev install` or `dev ext update --apply` accepts writes one `ext.accept`
   record to the audit trail, with the entry, the terminal (`tty`), and the parent processes it ran from, and sends
   a desktop notification. The acceptance list is written first: when it is busy, does not parse, or cannot be
   written (a full disk), nothing is accepted and the change stays as drift.
2. The next session start and `dev status` list them: `extensions accepted since your last session: …`, each entry
   cut to one short line without control, bidirectional, or zero-width characters. In Claude Code, the session start
   shows the list to you as a message, not only to the agent.
3. If you ran that `dev install`, run `dev ext ack` in your terminal. It clears the list and writes one `ext.ack`
   record per entry, with its terminal and parent processes; the next Claude Code session start shows you once when
   and from which terminal it ran. The guard refuses `dev ext ack` to agents in the forms it can read, and
   `dev ext ack` itself checks that it runs in your terminal. Neither proves that you typed it: see the known
   limits below.
4. If you did not, do not acknowledge it. Remove what it accepted (for a Codex MCP server, `codex mcp remove <name>`
   in your terminal), and read `dev audit --action ext.accept` to see which process accepted it.

**Known limits.** devkit decides that you ran `dev install`, `dev ext update --apply`, or `dev ext ack` from the
process tree and the terminal, and a process an agent starts through a launcher can look the same: `screen` or
`tmux`, `expect`, AppleScript's `do script`, `open` of a Terminal window or a `.command` file, or `launchctl`. The
guard refuses the forms it can read (it decodes the octal escapes such as `\015` and the caret controls such as `^m`
that `screen -X stuff` types), not every one: a script file or a program that builds the command at run time gets
through. The desktop notification, the session-start message, and the audit trail are the backstop until a
kernel-level check of who typed the command lands in 26.11.5.

The acceptance notification is a security notice: `DEVKIT_NO_NOTIFY=1` and `progress.notify: false`, which an agent
can set, do not turn it off. It shows only on macOS, and only while notifications are allowed for Script Editor, the
app macOS lists `osascript` notifications under (System Settings › Notifications). Without it, the session-start
message and the audit trail still show each acceptance.

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
  make the catalog invalid.

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

3. If a kind of work should require it, add or change a rule in `config/skill-rules.json`.
4. Run `dev install --only skills`, then `dev doctor`.
5. Record the change in `CHANGELOG.md` and in `NOTICE`.

A devkit skill is a folder with a `SKILL.md` (frontmatter `name` and `description`) in `plugin/skills/`.

See also [Agents](agents.md), [Configuration](configuration.md), and [Models and tokens](models-and-tokens.md).
