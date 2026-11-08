---
name: workspace
description: How to work with the ~/Development layout and the `dev` CLI—clone or create projects, run parallel work in managed git worktrees, find projects, clean up disk, start Docker, and enable a project's vendor AI tooling (Laravel Boost, Symfony AI Mate, FastAPI library-skills, Next.js MCP, Angular AI config). Use when cloning, creating, moving or locating a repository, when parallel work needs a worktree, when merged branches or worktrees pile up, when disk is full, or when setting up a new project's agent tooling.
---

# devkit workspace

## Layout

| What | Where | Command |
| --- | --- | --- |
| GitHub repo | `~/Development/<owner>/<repo>` (lowercase) | `dev clone <owner/repo>` |
| GitLab, Bitbucket, Azure DevOps, self-hosted | `~/Development/<host>/<group>/<…>/<repo>` | `dev clone <https-or-ssh-url>` |
| No remote yet | `~/Development/_local/<name>` | `dev new <name>` (`--github` creates it on GitHub) |
| Worktree | `~/Development/.worktrees/<project path>/<branch>` | `dev wt new <branch>` |
| Scratch | session scratchpad or `~/Development/.tmp/` | — |
| Map / notes / plans | `~/Development/.handoff/` | `dev sync` (automatic) |

Find a project: `dev go <query>` prints its path, and `dev go <query> <branch>` the path of its worktree for that
branch; `dev ls` lists all; `dev status` describes the current one.

Never make a sibling copy of a repository (`repo-pr12`, `repo-copy`, `repo-ci`) or clone outside this layout: a second
branch at once is a worktree. Moving or renaming a repository is `dev adopt`, with the developer.

## Parallel work with worktrees

Only when the user asks or two sessions must change the same repository at once:

```bash
dev wt new feat/short-topic          # from inside the repo; branches from origin/<default>
dev wt new fix/bug-123 --base origin/release-2.4
dev wt ls                            # this repo; --all for every repo
dev wt rm feat/short-topic           # refuses if there are uncommitted or unpushed changes
```

`dev wt new` copies `node_modules` copy-on-write when the lockfile matches (no reinstall), and copies the files
listed in `.worktreeinclude` (for example `.env`). Python: run `uv sync` in the new worktree.

A worktree lives as long as its branch. Once the branch is merged (by ancestry, by a merged PR, or by content), it
goes: `dev pr merge` deletes the branch and names a worktree that still holds it, and the nightly `gc` job removes
merged worktrees that are clean. `dev branch prune` lists every merged branch here and on origin with what keeps the
others, and `--apply` deletes them with their clean worktrees; the current branch, a branch with an open PR, an
embargoed branch, and a worktree with uncommitted changes or a live session are kept. Remove the worktree of an
abandoned branch with `dev wt rm <branch>`.

## Disk

`dev gc` (dry run) shows merged/contained worktrees, regenerable build output in idle worktrees and stale temp;
`dev gc --apply` reclaims it. It never touches uncommitted or unpushed work, live sessions, or main checkouts. The
nightly `gc` job applies the safe cases automatically. `dev du` reports usage, Docker included.

## Docker

`dev docker up` starts Colima (Docker CLI context and `DOCKER_HOST` are preconfigured, Testcontainers works);
`dev docker status`, `dev docker down`. Never prune volumes.

## Vendor AI tooling per project (run inside the project, only when the user wants it)

- **Laravel:** `composer require laravel/boost --dev && php artisan boost:install` (choose Claude Code and Codex).
- **Symfony:** `composer require --dev symfony/ai-mate && vendor/bin/mate init`.
- **FastAPI:** `uvx library-skills` (Codex) and `uvx library-skills --claude` (Claude)—version-matched skill.
- **Next.js 16+:** the `next-devtools` MCP server. Only the developer adds it, in a terminal, and it then runs in
  every project on this Mac: `dev ext add mcp next-devtools --command "npx -y next-devtools-mcp@<exact version>"
  --license <its license> --why "…"`. The guard refuses `claude mcp add` and `codex mcp add` to agents, and a server
  added that way shows as drift.
- **New Angular app:** `ng new <name> --ai-config=claude-code,open-ai-codex`.
- **Large Java codebase:** in IntelliJ IDEA, go to Settings → AI → MCP Server → Auto-configure (Claude Code + Codex).

These write project files (AGENTS.md, .mcp.json, .codex/config.toml): show the user the diff before committing.
