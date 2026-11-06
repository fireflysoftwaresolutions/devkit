# Changelog

All notable changes to devkit. Versions follow CalVer `YY.MM.PATCH` (PATCH restarts at 0 each month); tags are
`vYY.MM.PATCH`. Sections follow [Keep a Changelog](https://keepachangelog.com/).

## [26.11.2] - 2026-11-06

### Added

- A security policy (`SECURITY.md`) with private vulnerability reporting on GitHub, and a contributing guide
  (`CONTRIBUTING.md`), now that the repository is public.

### Changed

- The release process in Developing devkit uses `dev pr merge`, `dev release`, and a check of the published archive.

### Fixed

- `dev agents test` reads the hooks log from the byte offset it recorded, so the guard check no longer reports
  "no guard decision logged" when earlier lines hold multi-byte characters.
- `dev ci --run --resume` reruns the steps that read every file (secret, filesystem, and docs scans) after a
  docs-only change: a document can leak a key too. Builds and code linters still keep the docs shortcut.
- The Stop hook asks for `dev plan revise` only for the plans of the project the session works in. With two sessions
  open, it no longer sends one back over a plan the other changed in another project.
- `dev doctor` compares the installed instructions (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`) and the Codex crew
  roles with what this devkit writes, so an outdated install shows as out of date instead of passing because the
  files exist.
- The installed-plugin check compares the newest copy by version number, so 26.11.10 counts as newer than 26.11.2.

## [26.11.1] - 2026-11-06

devkit is open source under the Apache License 2.0.

### Added

#### Testing

- **`dev test`** runs the tests that failed last time first, then only the tests your changes can affect (the blast
  radius), with pytest, unittest, Jest, Vitest, Maven, Gradle, Go, PHPUnit, or Pest. `dev test run` takes paths,
  `--failed`, `--affected`, or `--all`; `dev test plan` shows what would run and why; `-p <project>` works from
  anywhere. A selection that runs no test exits with `2` instead of passing.
- **Tests that can fail.** `dev test audit` finds tests that cannot fail for a real bug (no or trivial assertions,
  empty bodies, swallowed failures, skips without a reason, sleeps, mock-only and smoke-only tests) in Python,
  JavaScript and TypeScript, Java and Kotlin, Go, and PHP. `dev test prove` checks that each new test fails without
  your change and passes five times with it. The Stop hook sends back a development turn whose new or changed tests
  cannot fail.
- **`dev ci --run --resume`** repeats only the failed steps, the steps that never ran, and what the fix affects. Its
  evidence combines the full run with the affected tests, and the pull request says so.
- The `testing` skill: a test strategy per kind of code, TDD, flaky tests, mutation testing and diff coverage, and
  diagnosing an existing suite. Required whenever a turn writes or runs tests.

#### CI/CD

- **`dev ci audit`** diagnoses the pipelines of GitHub Actions, GitLab CI, Bitbucket Pipelines, and Azure Pipelines:
  security, reliability, speed, quality gates, and delivery, each finding with a severity and its fix, merged with
  actionlint, zizmor, and `glab ci lint`. `--fix` applies the safe GitHub fixes (permissions, auto-cancel, job
  timeouts, actions pinned by SHA) and prints the snippets for the other hosts; `--strict` fails on medium findings.
- **`dev ci init`** writes a pinned pipeline for the detected stack and host: lint, tests with a JUnit report,
  security scans, and on release tags an SBOM, signing or attestation, and a check of what was published. On GitHub
  it writes `ci.yml`, a cache-free `release.yml`, and `dependabot.yml`; GitLab signs keyless, Bitbucket and Azure with
  a cosign key.
- **`dev ci watch`** starts after every push and pull request, follows the runs to the end, reads the first error of
  a failure, sorts it into a class, and gives one next action. The main session gets it as a crew message and hands
  fixes that are not trivial to Link.
- The delivery-policy field `job_timeout_minutes` (default 30) sets the job timeout that `dev ci audit` suggests,
  `--fix` writes, and `dev ci init` generates; a generated test job runs the policy's test gate.
- The `cicd` skill: fast, reliable, DevSecOps pipelines on any host. Required whenever a turn changes a pipeline.
- devkit's own CI runs a security job (gitleaks, semgrep, osv-scanner, trivy, zizmor), and `actionlint` joins the
  security tools.

#### The crew

- **25 specialist agents** named after The Matrix and led by the main session as Morpheus, from one roster
  (`config/crew.json`) for Claude Code, Claude Desktop, Codex (as roles), and the ChatGPT app. Each member has its own
  lane and time, the files it is assigned (the guard keeps its edits inside them), and a status block it must end its
  report with, which the SubagentStop hook checks (the crew canary). At most four members work at once.
- **`dev crew`**: the roster and who works now (`show`), briefs with the launch line (`brief`), routing (`route`),
  delegation (`delegate`, `decline`), the dispatcher (`next`), messages (`ask`, `answer`, `inbox`), decisions (`mode`,
  `decide`, `decisions`), and `sync` for the generated agents.
- **Guided or automatic.** In guided mode, decisions that are yours wait for you; in auto mode, the crew takes the
  recommended option and records it for review. Irreversible actions wait in both unless you pre-approved them.
- The `crew` skill: who to send for each kind of work, parallel or in sequence, briefs, and reports.

#### Progress

- **Time you can trust.** Active time (turns and subagents, never the wait for the developer), per-step estimates,
  dependencies (`after`), a critical-path ETA shown as a range, and a pace that one odd step cannot swing.
- **A plan that stays true.** Closing a step needs its evidence (`--check`) and a description that matches it;
  `dev progress add`, `reopen`, `skip`, `history`, and `assign --scope`; every change is recorded and audited.

#### Unfinished work

- **Build it for real.** `dev stubs` finds unfinished work in a change or a repository: `TODO` and `FIXME` markers,
  "not implemented" code, placeholders, and empty Python bodies. The Stop hook sends back a development turn that leaves
  untracked ones, and the guard refuses a commit that would include them. Planned work names its task (`TODO(T-…)`) or
  issue.
- The global rules: every test must be able to fail for a real bug; after a failure, re-run only the failed tests and
  what the fix affects; the full suite once, before the push; no stubs or placeholders in delivered work.

#### Surfaces

- Codex gets the plugin's slash commands as skills of the same name.
- New slash commands: `/devkit:progress`, `/devkit:crew-status`, `/devkit:test`, and `/devkit:stubs`.
- The Claude Desktop extension has 18 tools, among them `devkit_crew`, `devkit_stubs`, `devkit_test_plan`, and
  `devkit_ci_audit`.
- The dashboard's run card shows active time, the ETA range, the steps, and the crew's lanes.

### Changed

- **The handoff note stays current.** After every development turn with new commits, the Stop hook asks for a note
  and task update when your commits are newer than the note's last real change (task and session lines written by
  devkit do not count). It also works for sessions that started outside the repository, such as Claude Desktop's.
- The scout is now Sparks, a member of the crew; `scout` remains an alias in Claude Code and Codex.
- The UI skills are required for design work only: "open the dashboard" no longer asks for them.

### Fixed

- `dev sec scan` kept the leading dot of hidden paths in zizmor results: `./.github/workflows/ci.yml` no longer
  becomes `github/workflows/ci.yml`.
- The crew canary counts only subagents someone launched. Claude Code's own helpers, such as the compaction
  summary, are no longer counted as crew misses or sent back, and a report corrected after the reminder is
  recorded as corrected.
- `dev progress note` sends an update while a step is still running, so no update has to close a step; the
  silence reminder and the global rules now ask for a note, not a step, until the step is finished.
- `dev test prove <file>` proves only the file's new or changed tests, so tests that already existed are no
  longer reported as not testing your change.

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
  `dev doctor`. In a terminal it is a full-screen wizard driven with the arrow keys, with **Recommended setup** (what
  it detected, straight to the review) or **Customize**, a review with the packages per group and **Change a section**,
  and an apply step with live progress and the fix for each failure. A second run starts from the saved choices and
  marks what would change. Pipes, `TERM=dumb`, and `--yes` keep the line prompts; `--yes` accepts every default.
- **The `dev` home and banner:** the hand-drawn devkit wordmark with the amber spark as the dot of its `i`, and the
  tagline "Every repo in its place. Every agent on the same page." (also in the README hero and the plugin
  descriptions). The home shows the current project in three lines, the workspace in one, and the items that need
  attention, grouped, each with its command. Headings are a bold title with a rule.
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
  Its Claude Desktop and ChatGPT app checks prove that the apps' bundled agents use the same configuration and
  plugins as the terminal.
- **`dev agents`** reports installation, sign-in, plan, and quota for both agents, and `dev agents test` runs a real
  session and checks the hooks, the guard, the status block, the global instructions, and the devkit skills from
  recorded evidence.
- **Scheduled jobs** (`dev jobs`): `sync` (hourly), `sleep` (03:30), `gc` (04:00), `repair` (every 6 hours), `audit`
  (05:00), `doctor` (Mondays 09:00), and `lessons` (Fridays 16:00) as launchd agents `com.devkit.job.<name>`, generated
  from the `jobs` configuration key. `dev jobs show|run|logs|journal|set --every/--at/--weekly|enable|disable|install`.
  Each run has a timeout, a log, a journal entry, and an audit record, and a failure sends a notification. Custom jobs
  run any `dev` command. `dev doctor` checks that every enabled job is loaded and its last run passed. The jobs replace
  the single `com.devkit.sync` agent, which `dev install` removes.
- **Audit trail** (`dev audit`): every changing `dev` command, guard refusal, blocked settings change, missed status
  block, handoff write and push, settings repair, cleanup, and job run, with secrets redacted, in
  `~/.local/state/devkit/audit/YYYY-MM.jsonl`. The `audit` job publishes it daily to `audit/<user>@<host>/` in the
  handoff repository (`audit.publish`); `dev audit --team` reads every machine's. Filters: `--days`, `--action`,
  `--actor`, `--json`.
- **Docker on Colima** (vz, Rosetta, virtiofs) with Compose, Buildx, and Testcontainers settings; `dev docker`.
- **Git** identity and credentials from the GitHub CLI, shared defaults, and a global ignore file for agent and
  security-tool output.

#### Handoff, initiatives, and teams

- **Handoff repository** (default `<github-user>/handoff`, private, or shared by a team): `PROJECTS.md`, `TASKS.md`,
  `ACTIVITY.md`, `registry.json`, one note per project with frontmatter (status, priority, milestone, due date, tags,
  progress), generated auto and task blocks, plans, and an operations log.
- **Near-real-time sync:** every commit, push, merge, tag, rebase, PR, and release (by an agent or a `dev` command),
  every task, plan, lesson, and policy change, and every session start queue a background sync; events within about
  15 seconds share one run of about 3 seconds. The Stop hook writes the session line, renews locks, and publishes
  presence every 2 minutes, and the `sync` job runs hourly. A rejected push is rebased and retried once; a failure is
  audited and shown under Needs attention until a push succeeds.
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
- The rule applies to **every turn, without exception**: short answers, questions, status updates, replies during
  background work, and any language. The first turn stays a clean test of the instructions; a session that missed the
  block once gets a reminder in every later prompt, and replies corrected after a reminder are counted apart
  ("N corrected after a reminder" in `dev doctor`, `dev overview`, and `PROJECTS.md`).
- **Nothing comes before the block:** no verdict, correction, greeting, or comment on the last command before it;
  "lead with the point" applies below it, and a reply that waits for background work starts with it too. Every
  `communicating` template that can end a turn (kickoff, progress, status answer, decision, failure) starts with the
  block. `dev progress step` prints the block itself (Done, Check, Progress, Next, then the checklist), and its new
  `--check "<evidence>"` fills Check. The Stop hook says whether the block is missing or which line came before it.
- **Results per surface:** every session and status-block result records where it ran (terminal, Claude Desktop,
  the ChatGPT app, an IDE), and `dev doctor`, `dev overview`, and `PROJECTS.md` break the rate down by surface.
- devkit checks the marker after every turn, **scores the format**, stores a compact line as the project's latest
  status, and shows it in `dev status` (as a block with brand pills), `dev overview`, the dashboard, the session log
  and the next session's context. `ui.powerline` adds Nerd Font arrows between the pills. Older one-line forms and the
  legacy `DevKit ·` marker still count as present. `dev canary` reports the history.

#### Agents, models, and tokens

- **Global instructions** for both agents from one source, `agents/AGENTS.md`: the status block, method, plain
  writing, progress and time, token use, files and disk, worktrees, branches, Conventional Commits, CalVer, local-first
  delivery, backend practice, documentation, required skills, the team protocol, and handoff.
- **The devkit plugin** for Claude Code and Codex (`firefly-devkit` marketplace), with the devkit icon, display
  name, and the Firefly amber as Codex's brand color, and the skills `using-devkit`, `handoff`, `communicating`,
  `working-efficiently`, `workspace`, `delivery`, `release-policy`, `learning-loop`, `backend`, `devsecops`, `docs`, and
  `diagrams`; the slash commands `/devkit:status`, `overview`, `task`, `handoff`, `standup`, `ship`, `ci`, `dashboard`
  and `doctor`; and the read-only **`scout`** agent. `dev plugin validate` checks it with both agents' parsers.
- **Hooks** for both agents (SessionStart, UserPromptSubmit, PreToolUse (the guard), PostToolUse, SubagentStart,
  SubagentStop, Stop, and SessionEnd; Claude Code also ConfigChange and InstructionsLoaded.
- **Models and tokens:** the `working-efficiently` skill (routing, briefing templates, escalation, context hygiene),
  the `scout` role for Codex in `~/.codex/agents/scout.toml`, the Codex profiles `fast`, `deep`, and `review` in
  `~/.codex/<name>.config.toml`, at most 4 Codex subagent threads, and Claude's fast mode chosen per session. devkit
  never sets the main model. Codex's `review_model` makes `/review` use the strongest model, and
  `project_doc_max_bytes` = 131072 lets it read a project's AGENTS.md whole instead of stopping at 32 KiB. New keys:
  `claude.subagent_model`, `claude.subagent_effort`, `claude.fast_mode_per_session`, `codex.max_subagent_threads`,
  `codex.profiles`, `codex.subagent_model` and `codex.subagent_effort`.
- **Required skills:** `config/skill-rules.json` maps kinds of work to skills (UI, Angular, React, Spring, Java tests,
  APIs, databases, workflows, security, docs, diagrams, delivery, communication, lessons). The prompt hook names the skills a
  request needs, Claude subagent briefs get them appended, and the Stop hook sends a turn back once when it changed
  matching files without them. Configure with `skills.enforce` and `skills.rules`.
- **devkit always on:** a session that missed SessionStart gets the context at its first prompt; the guard refuses
  settings that turn off hooks or the plugin; `dev doctor` reports project overrides, sessions that ran with devkit,
  and stale plugin copies, which `dev install` reinstalls.
- **Configuration policy:** `dev config diff` previews drift, `repair` restores it (at session start and every 6 hours),
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
- **Both design skills for both agents:** devkit links `impeccable` for Claude Code and `frontend-design` for Codex,
  pinned, where the agent's plugin does not already bring them, and the UI rule requires both.
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
  pull requests (`templates/github/pull_request.md`, and `pull_request.es.md` in Spanish) and the handoff repository README (`templates/handoff/`).
- **Docs tooling:** `dev docs check` (markdownlint, lychee links and anchors, Vale with Google and write-good, SVG
  checks), the `docs` skill, and the `documentation-writer` and `doc-coauthoring` skills.
- **Diagrams:** `dev diagram render|check|new` renders validated SVG diagrams (flow, architecture, sequence, state,
  C4, ER, timeline) in the Firefly palette, dark and light, and the `diagrams` skill.

#### Git and GitHub

- **`dev branch new|rename|ls|clean`:** branches from an up-to-date `origin/<default>`, with names normalized to the
  standard (`feat/PROJ-142-invoice-export`), harness names renamed before the first push, and merged or gone branches
  cleaned after a dry run.
- **`dev commit`** makes Conventional Commits (known type, imperative mood, no final period, at most 72
  characters), never on the default branch, AI attribution removed, the secret scan applied; `--wip` and `--unwip`
  replace the shared stash.
- **`dev rebase`** on `origin/<default>` with a conflict report.
- **`dev ci --run`** runs the workflows' `run:` steps locally and records evidence for the exact files tested;
  `--mark-passed --note` records a suite you ran yourself; `--evidence` shows it.
- **`dev push`:** one push after the checks: branch name, fresh fetch, no WIP commits, not behind on a first push,
  local CI evidence, the push budget, and `--force-with-lease` that never drops someone else's commits.
- **`dev pr create|view|ls|checks|merge|ready|comment|review|diff`** opens PRs from the repository's template, or
  devkit's in English or Spanish (`templates/github/pull_request.es.md`, chosen by `language`), pre-filled with the
  commits, the local CI result, and the links; empty required sections are refused unless `--draft`. `dev pr checks --watch` waits
  and shows the failing jobs' trimmed logs. `dev pr merge` merges only when the checks and reviews allow it, with the
  repository's method and a clean squash message, then tidies the branches.
- **`dev run ls|view --failed|rerun --flaky`**, **`dev release`** (tag, push, and GitHub release by the delivery policy,
  with the CHANGELOG section as notes), and **`dev issue ls|view|create`**.
- **`dev git …` and `dev gh …`** run any git or gh command with the same checks.
- New configuration keys `commits.types` and `gitx.github_hosts`.

#### Lessons

- **`dev lesson add|ls|show|recur|prevent|retire|restore|scan|review`:** blameless lessons (what happened, root cause,
  fix, prevention, detection, evidence) per project, initiative, or globally, in `lessons/` in the handoff repository,
  versioned with their history. Sessions start with the known pitfalls that apply; the Stop hook asks once for a
  lesson after a fix or revert commit; `dev lesson scan` finds reverts, fixes, repeated guard refusals, missed status
  blocks, bouncing tasks, and developer corrections.
- **Use and decay:** every lesson's use is counted per machine and summed for the team; strength halves every 45 days
  without use. **`dev sleep`** (nightly) merges near-duplicates, archives retired, long-prevented, and faded lessons
  into `lessons/archive/` (`dev lesson restore` brings them back), keeps at most 25 active lessons per scope, proposes
  global lessons, and trims devkit's own old records, caches, and logs.
- **The `learning-loop` skill**, required for work about lessons, prefers prevention by test, lint, guard, skill, or
  CI over prose.

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
  history, live runs and time, search, a command palette and shareable URLs; regenerated on every sync. Visual first:
  rings, status bars, member waffles, sparklines, a calendar heatmap, CI strips, session bars, and lock countdowns,
  with details in accessible quick-look dialogs that have their own links.
- **Disk:** safe cleanup of merged worktrees, idle build output, stale temp files and the Docker build cache, checked
  again before every removal; copy-on-write `node_modules` for new worktrees; `dev gc` and `dev du`.
- **Identity:** the devkit wordmark outlined from Manrope with the amber spark as the dot of its letter, banners in
  the brand's light-trail system, the plugin icon, and the wordmark drawn by hand for the terminal.

- **Development work only.** devkit's process (the status block, required skills, reminders) applies to
  development turns; a recipe or consulting work with the Firefly consulting kit gets a normal answer. Each turn is
  judged by its request and by what it did (`lib/devkit/scope.py`); `DEVKIT_SCOPE=all` checks every turn.
- **Claude's quota.** A devkit status line (installed only when you have none) shows project, branch, model, context
  and the 5-hour and 7-day usage, and records the windows, so `dev doctor`, the self-test and the dashboard report
  Claude's quota like Codex's.
- **Status block first, always.** The communicating templates and `dev progress step` (new `--check`) now produce the
  block itself; the Stop hook says whether the block is missing or what came before it.
- `dev canary` shows results per surface and corrected misses; `dev help` lists `dev run`, `dev issue`, `dev git` and
  `dev gh`; `dev pr create --dry-run` prints the body alone on stdout; the wizard's sync interval sets the sync job.
- The guard protects the handoff repository's `audit/` folder.

- **Claude Desktop extension.** devkit ships an MCPB extension (`dev install --only desktop`, `dev desktop
  pack|status`) with its name and icon: 14 tools (overview, project status, tasks, standup, report, CI, lessons,
  delivery policy, team, doctor, audit, jobs, task add and update) for Desktop chat and Cowork, served by `dev mcp`
  from your checkout. `dev doctor` checks it next to the Code tab and the ChatGPT app's Codex.

### Security

- **The guard**, a pre-tool hook for both agents, enforces the workspace layout, managed worktrees, and canonical
  clones; blocks backup copies, writes outside the workspace and destructive commands (`rm -r` of protected paths,
  `git clean -x`, `git reset --hard` with changes); blocks plain force pushes, a third push of the same branch within
  an hour, blind CI re-runs, non-conforming branch names and AI attribution; keeps the handoff repository's work
  items, logs, presence, policies, and generated files out of direct edits; keeps each project session to its own
  records; steers `rm`, `unlink`, `find -delete`, `xargs rm`, `git clean -f`, and overwriting `cp` and `mv` to the
  recoverable `dev` commands; refuses tags that break the delivery policy; refuses a bare `git stash` (use
  `dev commit --wip`), raw `gh pr merge` and `gh release create` (use `dev pr merge` and `dev release`, or prefix
  `DEVKIT_RAW_GH=1`), and always `gh pr merge --admin`; refuses a commit, push, or tag chained after a pipe without
  `pipefail`; recognizes Copilot, Gemini, and Cursor co-author lines as attribution; and keeps `lessons/` out of
  direct edits.
  `dev guard check` simulates any decision.
- **No secrets in commits:** the guard runs gitleaks on what a `git commit` is about to record, including a `git add`
  earlier in the same command, and refuses the commit when it finds a secret.
