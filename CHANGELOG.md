# Changelog

All notable changes to devkit. Versions follow CalVer `YY.MM.PATCH` (PATCH restarts at 0 each month); tags are
`vYY.MM.PATCH`. Sections follow [Keep a Changelog](https://keepachangelog.com/).

## [26.11.0] - 2026-11-05

The first release.

### Added

#### Workspace and setup

- **Workspace layout** for any git host, with GitHub repositories in `~/Development/<owner>/<repo>`, other hosts in
  `~/Development/<host>/<group>/<repo>`, projects without a remote in `_local/`, managed worktrees in `.worktrees/`,
  and scratch files in `.tmp/`. `dev clone`, `dev new`, and `dev adopt` keep repositories in place.
- **Setup wizard** (`dev setup`, started by `install.sh`): it detects the Mac and both agents; asks for the workspace,
  the handoff repository (yours or a shared one), the agents and their permission modes, your stacks, your git hosts
  and clouds (preselected from what the Mac already shows), the Docker VM, automation, and the settings lock; shows
  the plan; installs only the packages you need; applies the configuration; offers the sign-ins; and runs
  `dev doctor`. `--yes` accepts every default.
- **Git hosts and clouds:** the configuration keys `hosts` (GitHub, GitLab, Bitbucket, Azure DevOps) and `clouds`
  (AWS, Azure, Google Cloud, DigitalOcean, Kubernetes, and OpenTofu or Terraform) choose which CLIs devkit installs, from
  Brewfile groups of the same names, and `dev doctor` checks that each is installed and signed in, with local checks
  only.
- **`dev install`**, safe to run again, in steps: `config`, `tools`, `cli`, `git`, `handoff`, `instructions`,
  `skills`, `claude`, `codex`, `docker`, `docs`, and `launchd`. The `tools` step installs only the packages that are
  missing for your stacks, git hosts, and clouds (`packages.auto`). `dev update` pulls devkit and applies it again.
- **`dev doctor`** checks the toolchain, GitHub sign-in and git identity, the workspace and handoff repository, the
  instructions and skills, Claude Code and Codex (settings, hooks, hook trust, plugins, MCP servers, profile models),
  sign-in and quota, the devkit plugin and whether each agent's copy is current, required skills, project settings
  that switch devkit off, sessions that ran with devkit, Docker, automation, the status-block canary and free disk.
- **`dev agents`** reports installation, sign-in, plan, and quota for both agents, and `dev agents test` runs a real
  session and checks the hooks, the guard, the status block, the global instructions, and the devkit skills from
  recorded evidence.
- **Docker on Colima** (vz, Rosetta, virtiofs) with Compose, Buildx, and Testcontainers settings; `dev docker`.
- **Git** identity and credentials from the GitHub CLI, shared defaults, and a global ignore file for agent and
  security-tool output.

#### Handoff, initiatives, and teams

- **Handoff repository** (default `<github-user>/handoff`, private, or shared by a team): `PROJECTS.md`, `TASKS.md`,
  `ACTIVITY.md`, `registry.json`, one note per project with frontmatter (status, priority, milestone, due date, tags,
  progress), generated auto and task blocks, plans, and an operations log. It syncs at the end of every session and
  hourly.
- **The hierarchy** portfolio → initiatives → projects → tasks, plans, and sessions, used by every command, the session
  context, the dashboard, and the reports.
- **Initiatives** (`dev initiative new|add|rm|ls|show`): cross-project goals with workstreams, cross-project tasks,
  decisions, risks, and progress that counts the initiative's items and linked member tasks. `dev handoff-import`
  adopts an older handoff repository from a staging folder; five were adopted this way during the rollout.
- **Work items** in `items/<scope>/<ID>.md`: one versioned file per task, feature, bug, or spike, with a changelog line
  and a commit for every change. `dev task add` (with `--type`, `--now`, `--later`, `--priority`, `--due`, `--why`,
  `--done`, `--claim`), `start` or `claim`, `release`, `done`, `block --why`, `unblock`, `cancel`, `move`, `edit`,
  `link --to <initiative>`, `show`, `history`, and `delegate --to`. `-p` and `-i` work before or after the verb.
- **Locks across machines:** `dev task start` takes an 8-hour lock and pushes it at once; other sessions are told
  not to work on the task; locks renew while a session works and expire when nobody renews them. Changes of scope need
  `--why`.
- **Delegation:** `dev task delegate` assigns a task to a teammate and writes the message to send, copied to the
  clipboard.
- **Versioned plans** (`dev plan new --kind plan|spec|spike|bugfix|adr`, `revise --why`, `approve`, `supersede`,
  `ls`): every change records a version, who, and why. The Stop hook asks for a revision when a plan changed without
  one.
- **Session logs** in `sessions/<scope>/YYYY-MM.md` (append-only), **presence** in `presence/<user>@<host>.json`, and
  merge rules in `.gitattributes`, so machines and developers do not conflict. `dev team` shows who is working on
  what. `dev handoff --log` adds a line.
- **`dev overview`**, **`dev standup`** and **`dev note set`** for the portfolio, the last days, and project metadata.

#### Progress, time, and communication

- **`dev progress start|step|block|done`**: live progress of agent work, with named steps, an ETA from the real pace,
  estimates calibrated from this project's history, updates published to the team, and desktop notifications when a
  run blocks or finishes or a long turn ends.
- **`dev time`**: turn lengths, estimate accuracy, subagent durations, and the slowest commands.
- **`dev report`**: a Markdown status report for the team or management, with health rules you can explain.
- **Hooks that keep you informed.** UserPromptSubmit answers "how is it going?" from real records, CI/CD pipelines
  included, and ignores background notifications. PostToolUse times slow commands and reminds a quiet agent after
  about 8 minutes. SubagentStart and SubagentStop track subagents.
- **The `communicating` skill** with templates for kickoff, progress, status, decisions, explanations, final and
  failure reports, pull request descriptions, squash-merge messages, review comments, delegation, and the structured
  formats of every record. Agents write in plain American English.

#### Status block

- Every final message starts with the **`[Firefly> DevKit]` status block**: a bold header with project, branch and
  task, then Done, Check (✓, ⚠, ✗ or –, with the evidence), Progress while a run is active, and Next.
- devkit checks the marker after every turn, **scores the format**, stores a compact line as the project's latest
  status, and shows it in `dev status` (as a block with brand pills), `dev overview`, the dashboard, the session log
  and the next session's context. `ui.powerline` adds Nerd Font arrows between the pills. Older one-line forms and the
  legacy `DevKit ·` marker still count as present. `dev canary` reports the history.

#### Agents, models, and tokens

- **Global instructions** for both agents from one source, `agents/AGENTS.md`: the status block, method, plain
  writing, progress and time, token use, files and disk, worktrees, branches, Conventional Commits, CalVer, local-first
  delivery, backend practice, documentation, required skills, the team protocol, and handoff.
- **The devkit plugin** for Claude Code and Codex (`firefly-devkit` marketplace) with the skills `using-devkit`,
  `handoff`, `communicating`, `working-efficiently`, `workspace`, `delivery`, `backend`, `devsecops`, `docs` and
  `diagrams`; the slash commands `/devkit:status`, `overview`, `task`, `handoff`, `standup`, `ship`, `ci`, `dashboard`
  and `doctor`; and the read-only **`scout`** agent. `dev plugin validate` checks it with both agents' parsers.
- **Hooks** for both agents (SessionStart, UserPromptSubmit, PreToolUse (the guard), PostToolUse, SubagentStart,
  SubagentStop, Stop, and SessionEnd; Claude Code also ConfigChange and InstructionsLoaded.
- **Models and tokens:** the `working-efficiently` skill (routing, briefing templates, escalation, context hygiene),
  the `scout` role for Codex in `~/.codex/agents/scout.toml`, the Codex profiles `fast`, `deep`, and `review` in
  `~/.codex/<name>.config.toml`, at most 4 Codex subagent threads, and Claude's fast mode chosen per session. devkit
  never sets the main model. New keys: `claude.subagent_model`, `claude.subagent_effort`,
  `claude.fast_mode_per_session`, `codex.max_subagent_threads`, `codex.profiles`, `codex.subagent_model` and
  `codex.subagent_effort`.
- **Required skills:** `config/skill-rules.json` maps kinds of work to skills (UI, Angular, React, Spring, Java tests,
  APIs, databases, workflows, security, docs, diagrams, delivery, communication). The prompt hook names the skills a
  request needs, Claude subagent briefs get them appended, and the Stop hook sends a turn back once when it changed
  matching files without them. Configure with `skills.enforce` and `skills.rules`.
- **devkit always on:** a session that missed SessionStart gets the context at its first prompt; the guard refuses
  settings that turn off hooks or the plugin; `dev doctor` reports project overrides, sessions that ran with devkit,
  and stale plugin copies, which `dev install` reinstalls.
- **Configuration policy:** `dev config diff` previews drift, `repair` restores it (at session start and hourly),
  `lock` installs a Claude Code managed-settings file, and ConfigChange refuses edits to managed keys.

#### Skills, plugins, and security

- **Curated skills** pinned to upstream commits for architecture (ADRs, system design, C4, domain modeling),
  Angular, React, Spring (including security and observability), testing, documentation, incident response,
  GitHub Actions hardening, Dependabot, threat modeling, Trail of Bits security skills, and OpenTelemetry. The Trail of
  Bits skills (CC-BY-SA-4.0) are credited in `NOTICE`.
- **Optional skill groups** in `skills.groups`: `security-deep`, `postgres`, and `observability` (on by default), and
  `codeql`, `iac`, `load-testing`, `contract-testing`, and `skill-review`.
- **Plugins.** Superpowers, context7, frontend-design, commit-commands, security-guidance (tuned),
  claude-md-management, claude-security, and the TypeScript, Python, Java, and PHP language servers for Claude Code;
  Superpowers, impeccable, and codex-security for Codex. Chrome DevTools and Angular CLI MCP servers, pinned.
- **`dev sec scan`:** secrets (gitleaks), dependencies (osv-scanner), code (Semgrep), filesystem and IaC (Trivy) and
  GitHub Actions workflows (zizmor), with scanner telemetry, and update checks off.

#### Delivery, docs, and diagrams

- **Delivery:** `dev ci` shows what CI runs so you run it locally and push once; `dev calver` prints the current and
  next CalVer version.
- **Delivery policy:** `dev delivery` detects how a project versions, tests, releases, and ships (tags, version files,
  release tools, changelog, CI, the test gate, branch model, what it publishes and deploys), recommends a policy that
  keeps what works (SemVer for libraries, CalVer for apps and services), and lists the gaps. `dev delivery apply
  --why` records it in `policies/<scope>.json` in the handoff repository, versioned with its history, and `--tasks`
  opens a work item per gap. Sessions see the policy, the guard refuses tags that break it, and release work requires
  the new `release-policy` skill. Initiatives can record a release train. devkit's own policy is recorded.
- **CI/CD status on any host:** `dev ci status [--watch]` shows the runs of the branch on GitHub Actions, GitLab CI,
  Bitbucket Pipelines, and Azure Pipelines, with jobs, the current step, elapsed time, and an ETA from recent
  successful runs. `dev status` shows the latest run, and status answers include the pipelines. GitHub is verified
  live; the other three are verified against recorded API responses.
- **devkit's own CI:** a pinned macOS workflow that runs the suite on Python 3.9 and 3.13, the devkit pull request
  template, and grouped weekly Dependabot updates for the actions.
- **Templates** for plans, specs, spikes, bug fixes and ADRs (`templates/plans/`), typed tasks (`templates/tasks/`),
  pull requests (`templates/github/pull_request.md`) and the handoff repository README (`templates/handoff/`).
- **Docs tooling:** `dev docs check` (markdownlint, lychee links and anchors, Vale with Google and write-good, SVG
  checks), the `docs` skill, and the `documentation-writer` and `doc-coauthoring` skills.
- **Diagrams:** `dev diagram render|check|new` renders validated SVG diagrams (flow, architecture, sequence, state,
  C4, ER, timeline) in the Firefly palette, dark and light, and the `diagrams` skill.

#### Files

- **Token-light file commands:** `dev files` (a compact tree), `dev find` (names), `dev grep` (contents, structural
  search with ast-grep), and `dev diff` (changed files first, short hunks, generated files folded) keep their output
  compact and capped. Homebrew adds fd, ast-grep, and difftastic.
- **Safe file operations:** `dev rm` moves paths to the Trash with a manifest and `dev rm --undo`, stages tracked
  deletions like `git rm`, and refuses open files, unsaved or untracked work, repositories, worktrees, and workspace
  and system folders unless the exact flag is given. `dev mv` and `dev cp` never overwrite silently, use `git mv` for
  tracked files, and refuse layout breaks, backup names, and heavy copies.

#### Dashboard and disk

- **Dashboard:** a self-contained, branded HTML command center with a clear information hierarchy: a summary Home
  (what needs you, team now, initiatives, since yesterday) and Initiatives, Projects, Team, Tasks, Activity, Explorer,
  and Health views, initiative and project pages, a task drawer with lock state, commands and
  history, live runs and time, search, a command palette and shareable URLs; regenerated on every sync.
- **Disk:** safe cleanup of merged worktrees, idle build output, stale temp files and the Docker build cache, checked
  again before every removal; copy-on-write `node_modules` for new worktrees; `dev gc` and `dev du`.
- **Identity:** the devkit wordmark outlined from Manrope with the amber spark as the dot of its letter, banners in
  the brand's light-trail system, the plugin icon, and the logo as terminal art.

### Security

- **The guard**, a pre-tool hook for both agents, enforces the workspace layout, managed worktrees, and canonical
  clones; blocks backup copies, writes outside the workspace and destructive commands (`rm -r` of protected paths,
  `git clean -x`, `git reset --hard` with changes); blocks plain force pushes, a third push of the same branch within
  an hour, blind CI re-runs, non-conforming branch names and AI attribution; keeps the handoff repository's work
  items, logs, presence, policies, and generated files out of direct edits; keeps each project session to its own
  records; steers `rm`, `unlink`, `find -delete`, `xargs rm`, `git clean -f`, and overwriting `cp` and `mv` to the
  recoverable `dev` commands; and refuses tags that break the delivery policy.
  `dev guard check` simulates any decision.
- **No secrets in commits:** the guard runs gitleaks on what a `git commit` is about to record, including a `git add`
  earlier in the same command, and refuses the commit when it finds a secret.
