# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""devkit: one predictable workspace for developers, Claude Code and Codex.

Modules:
  core      paths, configuration, shell/git helpers, process detection
  ui        terminal colours, banner, tables
  registry  project discovery and inspection, PROJECTS.md, handoff notes
  guard     pre-tool-use workspace rules shared by Claude Code and Codex
  hooks     hook entry points (session-start, stop, session-end, guard)
  gc        safe disk reclamation (merged worktrees, build output, temp)
  setup     `dev install` and `dev doctor`
  codexrpc  JSON-RPC client for `codex app-server`
  cli       the `dev` command
"""
