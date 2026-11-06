<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/hero-dark.webp">
  <img src="docs/images/hero-light.webp" alt="devkit: Every repo in its place. Every agent on the same page. By Firefly Software Solutions Inc." width="100%">
</picture>

# devkit

devkit is how Firefly Software developers work with Claude Code and Codex. Every repository is in a predictable
place. Every project has a living handoff note and a task board that a team, its Macs, and its agents share safely.
Every agent session starts from the current state, keeps you informed while it works, and ends by recording what
changed. The workspace never fills up with stray worktrees, sibling copies, or gigabytes of forgotten build output. One
install sets up the toolchain, git, Claude Code (CLI and Desktop), Codex (CLI and the ChatGPT app), skills, plugins,
Docker and the background automation, and keeps them that way.

[Why](#why-devkit) ·
[Install](#install) ·
[A quick tour](#a-quick-tour) ·
[From plan to operate](#from-plan-to-operate) ·
[Features](#features) ·
[For your agents](#for-your-agents) ·
[How it fits together](#how-it-fits-together) ·
[What's inside](#whats-inside) ·
[Docs](#documentation) ·
[Credits](#credits-and-license)

## Why devkit

| Without devkit | With devkit |
| --- | --- |
| Each session rediscovers the project | The session starts with the project's handoff note, tasks, locks, git state, commands, and the last status |
| Two people (or two agents) pick up the same task | `dev task start` locks it on every machine at once; every change records who, when, and why |
| "How is it going?" | Agents report progress with an ETA computed from their real pace, and you get a notification when they finish or get stuck |
| Worktrees and `repo-pr12` copies pile up; disks fill | One managed worktree location, a guard that blocks stray ones, and safe automatic cleanup once work is merged |
| Push → CI → fix → push → CI … | `dev ci` shows what CI runs; integrate locally, push once, one CI run; `dev ci status` follows it with an ETA on GitHub, GitLab, Bitbucket, or Azure |
| Every project releases its own way, or not at all | `dev delivery` detects and records a delivery policy (versioning, test gate, changelog, release flow); the guard refuses tags that break it |
| An agent's `rm -rf` or `cp` destroys work | `dev rm`, `dev mv`, and `dev cp` use the Trash, refuse open or unsaved files, and `dev rm --undo` restores them; the guard steers raw deletes to them |
| `claude/vigilant-jang-ec71c0` branches and AI attribution | Trunk-based naming (`feat/PROJ-142-invoice-export`) and no attribution, enforced for both agents |
| Claude and Codex behave differently, and settings drift | One set of rules, skills, and hooks for both; drift is blocked and repaired; `dev config diff` previews everything |
| Agents skip the skill that fits the job | Required skills are named at the prompt, added to subagent briefs, and checked before a turn ends |
| Nobody knows what is in flight | A shared handoff repository with initiatives, projects, task boards, a team view, status reports, and a dashboard |
| "Did the agent even read the rules?" | Every reply starts with the `[Firefly> DevKit]` status block; devkit checks it, scores it, records it, and reports deviations |

## Install

```bash
gh auth login -s repo,read:org,workflow,gist                      # once
gh repo clone fireflysoftwaresolutions/devkit ~/Development/fireflysoftwaresolutions/devkit
~/Development/fireflysoftwaresolutions/devkit/install.sh          # the setup wizard; safe to run again
exec zsh && dev doctor                                             # all green
```

`install.sh` makes sure Homebrew, Python, and the GitHub CLI are ready, then starts the setup wizard. The wizard asks
for your workspace, your handoff repository (yours, or one your team shares), which agents to configure, your stacks
and the Docker VM, shows the plan, and applies it: the toolchain for your stacks, git signed in with your GitHub
identity, Claude Code and Codex, the devkit plugin and the curated skills in both, and the hourly sync. Details are in
[Getting started](docs/getting-started.md).

## A quick tour

```text
dev                          your workspace at a glance
dev dashboard                the command center in the browser: initiatives, projects, team, tasks, activity, health
dev overview                 every initiative and project: status, progress, Now tasks, latest status
dcd billing                  jump into a project                     dwt feat/invoice-export   …or into a new worktree
dev task                     the project's board                      dev task add "Export invoices as CSV" --type feature --now
dev task start 2             claim it (locked on every machine)       dev task done 2      dev task release 2
dev task delegate 3 --to jordan --why "Jordan owns the event bus"     hand it over, with the message to send
dev plan new "Refund events" --kind spec                              a versioned spec; dev plan revise <file> --why "…"
dev initiative show payments one initiative across its projects       dev task -i payments
dev team                     who is working on what, on every machine
dev progress                 live runs with ETA                       dev time   where the time goes
dev report --days 7          a status report for the team, in Markdown
dev claude api -w feat/x     Claude Code in a managed worktree        dev codex api   Codex in the project
dev ci                       what CI runs; run it locally, then push once
dev ci status --watch        the branch's pipelines: jobs, current step, ETA (GitHub, GitLab, Bitbucket, Azure)
dev delivery                 the project's release policy and gaps     dev delivery apply --why "…"   record it
dev files · find · grep · diff   token-light views of the tree, names, contents and changes
dev rm build/out.log         to the Trash, refuses unsaved work        dev rm --undo   put it back
dev sec scan                 secrets, dependencies, code, IaC and workflows
dev gc                       what can be reclaimed safely (dry run)
dev doctor                   verify the whole setup                   dev agents test   a live check of both agents
```

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/dashboard/home-1440-dark.webp">
  <img src="docs/images/dashboard/home-1440-light.webp" alt="The devkit dashboard's Home view: the title says how many urgent items need you, four figures follow (PRs failing CI, blocked tasks, doing, live sessions), then the Needs you list with one amber copy button on the first row, Team now with each person's live session, progress, and locks, and one row per initiative with progress, milestone, and open and blocked tasks." width="100%">
</picture>

All commands are in [docs/commands.md](docs/commands.md).

## From plan to operate

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/diagrams/lifecycle-map-dark.svg">
  <img src="docs/images/diagrams/lifecycle-map-light.svg" alt="From plan to operate: plan with dev plan new and Superpowers; track the task with dev task; build with dev progress and the required skills; pass the local test gate with dev ci and dev sec scan; review with /code-review and the communicating templates; integrate on one branch with the delivery skill; release by the recorded policy with dev delivery and the release-policy skill; follow CI with dev ci status; deploy through a tag and its pipeline; hand off with the handoff skill and dev sync." width="100%">
</picture>

Every stage of the lifecycle has a devkit command and a skill, so nothing falls between them. The table with each
stage's guide is in [Concepts › From plan to operate](docs/concepts.md#from-plan-to-operate).

## Features

| Area | What you get | Guide |
| --- | --- | --- |
| Workspace | One canonical path per repository on any git host, managed worktrees, safe cleanup | [Concepts](docs/concepts.md) and [Disk and cleanup](docs/disk-and-cleanup.md) |
| Handoff | A private (or team) handoff repository with notes, the projects map, task boards, and activity, kept current by a sync after each session and every hour | [Concepts](docs/concepts.md#the-handoff-repository) |
| Teams and machines | Versioned work items with locks that every machine sees, reasons for every change of scope, versioned plans, append-only session logs, presence, `dev team` | [Teams and machines](docs/teams.md) |
| Initiatives | Cross-project goals with workstreams, cross-project tasks, honest progress, and adoption of older handoff repositories | [Initiatives](docs/initiatives.md) |
| Progress and communication | `dev progress` with an ETA from the real pace, calibrated estimates, reminders, notifications, `dev time`, message templates, `dev report` | [Progress and communication](docs/progress-and-communication.md) |
| Status block | Every reply starts with a `[Firefly> DevKit]` block (done, check, progress, next) that devkit checks, scores, and shows everywhere | [Concepts](docs/concepts.md#the-status-block) |
| Dashboard | A self-contained HTML command center: home, initiatives, projects, team, tasks, activity, explorer, health | [Dashboard](docs/dashboard.md) |
| Delivery | `dev ci`, local integration, one push and one CI run, CalVer, a push budget, and no blind re-runs | [Delivery](docs/delivery.md) |
| Delivery policy | `dev delivery` detects how a project versions, tests, and releases, recommends a policy, and records it; sessions see it and the guard enforces its tags | [Delivery › The delivery policy](docs/delivery.md#the-delivery-policy) |
| CI/CD status | `dev ci status [--watch]` for GitHub Actions, GitLab CI, Bitbucket Pipelines, and Azure Pipelines: jobs, current step, elapsed time, and an ETA | [Commands › dev ci status](docs/commands.md#dev-ci-status---watch---branch-name----all-branches---all-jobs---limit-n---json) |
| Files | `dev files`, `find`, `grep`, and `diff` with compact, capped output; `dev rm`, `mv`, and `cp` with the Trash, undo, and refusals instead of lost work | [Commands › Files](docs/commands.md#files) |
| Git hosts and clouds | The CLIs for GitLab, Bitbucket, Azure DevOps, AWS, Azure, Google Cloud, DigitalOcean, Kubernetes, and OpenTofu, installed and checked for sign-in | [Getting started](docs/getting-started.md#the-setup-wizard) |
| Security | `dev sec scan` (gitleaks, osv-scanner, Semgrep, Trivy, zizmor), a secret check before every commit, and the DevSecOps skills and plugins | [Skills and plugins](docs/skills-and-plugins.md), [Guard rules](docs/guard.md) |
| Guard | A pre-tool hook that blocks what wastes time or disk or breaks shared records, and explains the right way | [Guard rules](docs/guard.md) |
| Skills | A curated, pinned catalog for both agents, optional groups, and required skills enforced per kind of work | [Skills and plugins](docs/skills-and-plugins.md) |
| Models and tokens | Your model stays yours; a read-only `scout` agent, Codex profiles, a cap on parallel threads, and routing rules | [Models and tokens](docs/models-and-tokens.md) |
| Templates | Plans, specs, spikes, bug fixes, ADRs, typed tasks, and a pull request description | [Templates](docs/templates.md) |
| Docs and diagrams | `dev docs check`, plus validated, branded SVG diagrams in a dark and a light version | [Diagrams](docs/diagrams.md) |
| Docker | Colima with Compose, Buildx, and Testcontainers | [Docker](docs/docker.md) |

<!-- TODO(devkit-docs): files-and-git — add the git and GitHub wrapper (dev branch, commit, push, pr, run, release, issue) once it is merged. -->

## For your agents

devkit is also a plugin for **Claude Code** and **Codex**, installed automatically:

- **Context in, record out.** Each session receives the project's handoff note, tasks, locks, initiatives, branch,
  PRs, commands, and the last status; it ends with its commits, status, and session line recorded and the handoff
  repository pushed.
- **Kept informed.** Agents post a plan with an estimate, progress updates with an ETA, and a final report; the hooks
  answer "how is it going?" from real records and remind a quiet agent to update you.
- **Slash commands:** `/devkit:status` · `/devkit:overview` · `/devkit:task` · `/devkit:handoff` · `/devkit:standup` ·
  `/devkit:ship` · `/devkit:ci` · `/devkit:dashboard` · `/devkit:doctor` (skills of the same name in Codex).
- **Skills:** `using-devkit`, `handoff`, `communicating`, `working-efficiently`, `workspace`, `delivery`,
  `release-policy`, `backend`, `devsecops`, `docs`, and `diagrams`, plus a curated, pinned set for architecture,
  Angular, React, Spring, testing, security, and observability. Required skills are enforced for each kind of work.
- **Rules:** one set of global instructions for both agents: the status block, method, plain American English,
  progress and time, token use, files and disk, worktrees, branches, commits, delivery, backend practice, required
  skills, the team protocol, and handoff.
- **Guard:** a pre-tool hook that blocks what wastes time or disk, and explains the right way in one sentence.

## How it fits together

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/diagrams/architecture-dark.svg">
  <img src="docs/images/diagrams/architecture-light.svg" alt="How devkit fits together: the devkit repo installs rules, the devkit plugin, and hooks into Claude Code (CLI and Desktop) and Codex (CLI and the ChatGPT app); the agents work in your repositories and exchange context and session records with the private handoff repo, which an hourly sync keeps current; repositories and the handoff repo are pushed to GitHub, and the dashboard and overview read the handoff repo." width="100%">
</picture>

The ideas behind it are in [docs/concepts.md](docs/concepts.md).

## What's inside

| Path | What |
| --- | --- |
| `bin/dev`, `lib/devkit/` | The `dev` CLI (Python 3.9+, standard library only) |
| `plugin/` | The devkit plugin for Claude Code and Codex: skills, slash commands, the `scout` agent, identity |
| `agents/AGENTS.md` | The global instructions generated into `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` |
| `config/` | What devkit manages, such as Claude and Codex settings, hooks, plugins, MCP servers, profiles, pinned skills, skill rules, git, and docs defaults |
| `templates/` | Plan, spec, spike, bug-fix, and ADR templates; task templates; the pull request template; the handoff repository README |
| `install.sh`, `Brewfile` | Bootstrap and toolchain |
| `shell/` | zsh integration (`dcd`, `dwt`, completion, Docker environment) |
| `tools/brand/` | Renders devkit's icon and banners from the Firefly brand elements |
| `docs/` | The guides, diagram specs, and images |
| `tests/` | Unit tests (`python3 -m unittest discover -s tests`) |

## Documentation

| Guide | For |
| --- | --- |
| [Getting started](docs/getting-started.md) | Install on a new Mac, the setup wizard, onboard a developer, update, uninstall |
| [Concepts](docs/concepts.md) | Layout, how work is organized, the handoff repository, worktrees, the guard, the status block, sync |
| [Teams and machines](docs/teams.md) | Work items and locks, reasons, and history, versioned plans, session logs, presence, merge rules |
| [Initiatives](docs/initiatives.md) | Cross-project initiatives, progress, adopting an older handoff repository |
| [Progress and communication](docs/progress-and-communication.md) | `dev progress`, ETA and calibration, notifications, `dev time`, message templates, status reports |
| [Commands](docs/commands.md) | Every `dev` command and option, including the file commands and `dev ci status` |
| [Dashboard](docs/dashboard.md) | The HTML command center: views, pages, the task drawer, shortcuts, routes |
| [Configuration](docs/configuration.md) | Every key of `~/.config/devkit/config.json`, personal rules, environment variables |
| [Agents](docs/agents.md) | What is configured in Claude Code and Codex, hooks, devkit always on |
| [Skills and plugins](docs/skills-and-plugins.md) | The devkit plugin, required skills, the curated catalog, what was left out |
| [Models and tokens](docs/models-and-tokens.md) | Models, effort, subagents, the scout agent, Codex profiles |
| [Templates](docs/templates.md) | Plans and specs, tasks, the pull request template |
| [Delivery](docs/delivery.md) | Local-first GitOps: the delivery policy, one integration, one push, one CI run, following CI, releases |
| [Guard rules](docs/guard.md) | Everything the guard blocks, and why |
| [Diagrams](docs/diagrams.md) | `dev diagram` and the diagram design rules |
| [Disk and cleanup](docs/disk-and-cleanup.md) | `dev gc` safety rules and automatic cleanup |
| [Docker](docs/docker.md) | Colima, Compose, Buildx, Testcontainers |
| [Troubleshooting](docs/troubleshooting.md) | Logs, common problems, resetting |
| [Developing devkit](docs/development.md) | Code layout, tests, brand assets, release process |

## Credits and license

devkit is designed and maintained by **Andrés Contreras Guillén** for **Firefly Software Solutions Inc.**

Copyright © 2026 Firefly Software Solutions Inc. All rights reserved. Proprietary; see [LICENSE](LICENSE).
Third-party skills and tools installed by devkit keep their own licenses; see [NOTICE](NOTICE).
