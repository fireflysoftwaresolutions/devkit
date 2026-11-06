# Changelog

All notable changes to devkit. Versions follow [Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-11-05

First release.

### Added
- `dev` CLI: dashboard, projects (`ls`, `go`, `status`, `clone`, `new`, `adopt`, `open`), agents (`claude`, `codex`,
  `handoff`), worktrees (`wt new|ls|rm`), delivery (`ci`), tracking (`sync`, `canary`), disk (`gc`, `du`),
  environment (`doctor`, `install`, `update`, `docker`, `version`).
- Canonical workspace layout `~/Development/<owner>/<repo>`, `_local/`, `.worktrees/`, `.tmp/`, `.handoff/`.
- Private handoff repository (configurable, default `<github-user>/handoff`): live `PROJECTS.md`, per-project
  handoff notes with automatic session logs, activity log; hourly background sync via launchd.
- Shared global instructions for Claude Code and Codex (`agents/AGENTS.md`), including local-first delivery
  (one integration, one push, one CI run), backend methodology and the `DevKit ·` instruction canary.
- Hooks for Claude Code and Codex: session context, guard (layout, worktrees, clones, backups, destructive
  commands, force pushes, CI push budget, blind CI re-runs, AI attribution), session tracking, canary.
- Safe disk reclamation: merged worktrees, regenerable build output in idle worktrees, stale temp, Docker build cache.
- Curated skills for both agents (pinned upstream commits) and devkit's own skills: `devkit-handoff`,
  `devkit-workspace`, `devkit-delivery`, `devkit-backend`.
- Claude Code: bypassPermissions by default, no attribution, plugins (Superpowers, context7, frontend-design,
  commit-commands, security-guidance, TypeScript/Python/Java/PHP LSP), MCP (Chrome DevTools, Angular CLI).
- Codex (CLI and ChatGPT app): approval never + full access, hooks trusted through Codex's API, plugins
  (Superpowers, impeccable), MCP (context7, Chrome DevTools, Angular CLI).
- Git identity and credentials from the GitHub CLI; shared git defaults and global ignore.
- Docker on Colima (vz + Rosetta + virtiofs) with Compose/Buildx and Testcontainers settings.
- Bootstrap `install.sh` with Brewfile and npm globals; zsh integration (`dcd`, `dwt`, completion).
