# Changelog

All notable changes to devkit. Versions follow CalVer `YY.MM.PATCH` (PATCH restarts at 0 each month); tags are
`vYY.MM.PATCH`. Sections follow [Keep a Changelog](https://keepachangelog.com/).

## [26.11.0] — 2026-11-05

First release.

### Added

- **Workspace layout** for any git host: GitHub repositories in `~/Development/<owner>/<repo>`, other hosts in
  `~/Development/<host>/<group>/<repo>`, projects without a remote in `_local/`, managed worktrees in `.worktrees/`,
  scratch in `.tmp/`.
- **Handoff repository** (configurable; default `<github-user>/handoff`, private): live `PROJECTS.md`, `TASKS.md`,
  `ACTIVITY.md`, `registry.json`, one note per project with frontmatter (status, priority, milestone, due, tags), a
  Now/Next/Later/Done task board, automatic session lines, plans and an operations log; synced at the end of every
  session and hourly.
- **`dev` CLI**: dashboard, `ls`, `go`, `status`, `clone`, `new`, `adopt`, `open`; `claude` and `codex` launchers with
  managed worktrees; `handoff`, `task`, `note`, `overview`, `standup`; `wt new|ls|rm`; `ci`, `calver`; `sync`, `gc`,
  `du`, `canary`; `dashboard`; `docs check`; `config diff|repair|show|edit|validate|lock|unlock|status`;
  `guard check`; `plugin validate`; `agents` and `agents test`; `setup`, `install`, `update`, `doctor`, `docker`.
- **Setup wizard** (`dev setup`, run by `install.sh`): detects the machine and both agents, asks for workspace,
  agents and permission modes, stacks, Docker VM, automation and settings lock, previews the plan, installs only the
  chosen stacks, applies and verifies.
- **devkit plugin for Claude Code and Codex** with its own marketplace (`firefly-devkit`): skills `handoff`,
  `workspace`, `delivery`, `backend`, `docs`, `diagrams`; slash commands `/devkit:status`, `overview`, `task`,
  `standup`, `ship`, `ci`, `dashboard`, `doctor`; Firefly-branded identity; validated by Claude Code's validator and
  Codex's own manifest parser.
- **Global instructions** generated for both agents from one source: method, files and disk, worktrees, trunk-based
  branch naming, Conventional Commits, CalVer, local-first delivery (one integration, one push, one CI run), backend
  methodology, documentation, handoff and the `DevKit ·` status line.
- **Hooks** for both agents: session context (handoff digest, tasks, git state, commands, last status line), the
  guard, Stop (canary, status capture, one-time handoff reminder after commits), SessionEnd sync; Claude Code also
  ConfigChange (blocks edits to managed settings) and InstructionsLoaded (canary diagnostics).
- **Guard**: workspace layout, worktree and clone locations, backup copies, destructive commands, `git clean -x`,
  `reset --hard` with changes, force pushes, the CI push budget, blind CI re-runs, branch naming and AI attribution;
  heredoc bodies and file-descriptor redirections are understood; `dev guard check` simulates any decision.
- **Instruction canary and status line**: every reply starts with `DevKit · project@branch · outcome · verification
  · next`; recorded per agent, shown in notes, context, overview and the dashboard.
- **Configuration policy**: `dev config diff` previews drift, `repair` restores it (automatically at session start
  and hourly), `lock` installs a Claude Code managed-settings drop-in.
- **Dashboard**: a self-contained, branded HTML command centre (home, projects, tasks, activity, explorer, health)
  with search, a command palette and shareable state; regenerated on every sync.
- **Providers**: `dev agents` reports installation, sign-in, plan and quota for Claude Code and Codex;
  `dev agents test` runs a real session and verifies the integration from recorded evidence.
- **Diagrams**: `dev diagram render|check|new` renders validated SVG diagrams (flow, architecture, sequence, state,
  C4, ER, timeline) in the Firefly palette, dark and light.
- **Docs tooling**: `dev docs check` (markdownlint, lychee links and anchors, Vale with Google and write-good),
  pinned `documentation-writer` and `doc-coauthoring` skills, the `claude-md-management` plugin.
- **Curated skills** pinned to upstream commits for architecture (ADRs, system design, C4, domain modelling),
  Angular, React, Spring and testing; plugins Superpowers, context7, frontend-design, commit-commands,
  security-guidance (tuned) and the TypeScript, Python, Java and PHP language servers; Chrome DevTools and Angular CLI
  MCP servers.
- **Disk**: safe garbage collection of merged worktrees, idle build output, stale temp and Docker build cache, with
  re-verification before every removal; copy-on-write `node_modules` for new worktrees.
- **Docker on Colima** (vz, Rosetta, virtiofs) with Compose, Buildx and Testcontainers settings.
- **Git** identity and credentials from the GitHub CLI; shared defaults and a global ignore for agent artefacts.
- **Identity**: the devkit wordmark outlined from Manrope with the amber spark dotting the i, banners in the
  brand's light-trail system, the plugin icon and the logo as terminal art.
