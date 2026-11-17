# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""devkit: one predictable workspace for developers, Claude Code and Codex.

Modules:
  core      paths, configuration, shell/git helpers, process detection
  ui        terminal colours, banner, tables
  registry  project discovery and inspection, PROJECTS.md, handoff notes
  hooks     hook entry points (session-start, pre-tool, stop, session-end, …)
  gc        safe disk reclamation (merged worktrees, build output, temp)
  setup     `dev install` and `dev doctor`
  codexrpc  JSON-RPC client for `codex app-server`
  cli       the `dev` command
"""
