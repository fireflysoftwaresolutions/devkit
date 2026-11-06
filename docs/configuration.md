# Configuration

This page lists every setting devkit reads: the keys of your configuration file, your personal agent rules, the
environment variables, and the files in the devkit repository that decide what is installed for everyone.

## `~/.config/devkit/config.json`

`dev setup` (or the first `dev install`) creates it. Every key is optional; missing keys take the defaults below.
Nested objects are merged with the defaults, so you write only what you change.

```json
{
  "dev_root": "~/Development",
  "github_user": "maria",
  "handoff_repo": "maria/handoff",
  "skills": { "groups": ["security-deep", "postgres", "observability", "iac"] },
  "ui": { "powerline": true }
}
```

Run `dev config edit` to open it and validate it, `dev config show` to print the effective configuration, and
`dev install` (or the step named in the tables) to apply a change.

### Workspace

| Key | Default | Meaning |
| --- | --- | --- |
| `dev_root` | `~/Development` | Root of the workspace |
| `github_user` | detected with `gh api user` | Your GitHub login |
| `handoff_repo` | `<github_user>/handoff` | The private repository that holds the map, notes, work items, and logs. Set it to a shared repository for a team (see [Teams and machines](teams.md#set-up-a-shared-handoff-repository)) |
| `handoff_dir` | `<dev_root>/.handoff` | Local clone of the handoff repository |
| `default_host` | `github.com` | Host whose repositories live at `<owner>/<repo>`; all others live at `<host>/<group…>/<repo>` |
| `host_dirs` | `{}` | Folder aliases for hosts, for example `{"gitlab.com": "gitlab"}` → `~/Development/gitlab/<group>/<repo>` |
| `editor` | `code` | Editor for `dev open` (and for `dev handoff --edit` and `dev config edit` when `$EDITOR` is unset) |
| `stacks` | `["java", "python", "php", "node"]` | The stacks whose packages `dev setup` and `dev install --only tools` install |
| `hosts` | `["github"]` | Git hosts you use: `github`, `gitlab`, `bitbucket`, `azure-devops`. devkit installs their CLIs, `dev doctor` checks the sign-in, and `dev ci status` reads their pipelines |
| `clouds` | `[]` | Clouds and infrastructure tools you use: `aws`, `azure`, `gcp`, `digitalocean`, `kubernetes`, `terraform` (OpenTofu). devkit installs their CLIs and `dev doctor` checks the sign-in |
| `packages.auto` | `true` | `dev install` installs missing packages for your stacks, hosts, and clouds; `false` only reports them |
| `language` | `en-US` | The language for replies and documents, which the `communicating` skill asks agents to follow, and the language of the pull request template that `dev pr create` fills (`es-ES` uses the Spanish one). devkit does not put it in the session context, so also state another language in your [personal agent rules](#personal-agent-rules) |

Changing `dev_root`, `default_host`, or `host_dirs` changes every canonical path: run `dev ls` and `dev adopt` the
repositories that are no longer in place.

### Agents

| Key | Default | Meaning |
| --- | --- | --- |
| `agents.claude` · `agents.codex` | `true` | Configure Claude Code or Codex. `false` skips that agent in `dev install` and `dev doctor` |
| `claude.default_mode` | `bypassPermissions` | Claude Code's permission mode: `bypassPermissions` (no prompts; the guard protects the workspace), `acceptEdits` or `default` |
| `claude.subagent_model` · `claude.subagent_effort` | empty | Model and effort for every Claude subagent. Empty keeps the session's (recommended). See [Models and tokens](models-and-tokens.md#configuration-keys) |
| `claude.fast_mode_per_session` | `true` | Keep `fastModePerSessionOptIn` in Claude's settings |
| `claude.statusline` | `true` | Install devkit's status line when you have none (it records Claude's quota; see [Agents](agents.md#settings-claudesettingsjson)) |
| `codex.mode` | `full` | The choice in `dev setup`: `full` (no prompts, full access) or `workspace` (sandboxed to the project, asks when it needs more) |
| `codex.approval_policy` · `codex.sandbox_mode` | `never` · `danger-full-access` | Written to `~/.codex/config.toml`; `workspace` mode sets `on-request` · `workspace-write` |
| `codex.max_subagent_threads` | `4` | Parallel subagent threads per Codex session |
| `codex.profiles` | `fast`, `deep`, `review` | Codex profiles: an object adds or replaces profiles by name; `false` writes none |
| `codex.subagent_model` · `codex.subagent_effort` | empty | Default model and reasoning effort for Codex subagents |
| `lock` | `false` | The choice in `dev setup` to lock Claude Code settings; the lock itself is `dev config lock` |

Apply agent changes with `dev install --only claude` or `dev install --only codex`.

### Skills

| Key | Default | Meaning |
| --- | --- | --- |
| `skills.groups` | `["security-deep", "postgres", "observability"]` | Optional skill groups to install in addition to the core set: `security-deep`, `codeql`, `postgres`, `observability`, `iac`, `load-testing`, `contract-testing`, `skill-review`, or `all`. See [Skills and plugins › Optional groups](skills-and-plugins.md#optional-groups) |
| `skills.enforce` | `true` | Enforce the skills each kind of work requires (see [Skills and plugins › Required skills](skills-and-plugins.md#required-skills)) |
| `skills.rules` | `[]` | Your own skill rules. A rule with the same `id` as a devkit rule changes the fields you give; a new `id` adds a rule |

Apply group changes with `dev install --only skills`. Rule changes apply at the next prompt.

```json
{
  "skills": {
    "rules": [
      { "id": "ui", "skills": { "claude": ["frontend-design"], "codex": ["impeccable", "frontend-design"] } },
      { "id": "terraform", "why": "Terraform style and tests", "files": ["*.tf"],
        "prompt": "\\bterraform\\b", "skills": { "claude": ["terraform-style-guide"], "codex": ["terraform-style-guide"] } }
    ]
  }
}
```

### Work, progress, and display

| Key | Default | Meaning |
| --- | --- | --- |
| `work.lock_hours` | `8` | How long a claimed task stays locked without renewal (see [Teams and machines](teams.md#locks-across-machines)) |
| `progress.nudge_minutes` | `8` | Minutes without an update to the developer before the hooks remind the agent |
| `progress.notify` | `true` | Desktop notifications when a run blocks or finishes, and when a long turn ends |
| `progress.notify_after_minutes` | `10` | A turn at least this long sends a notification when it ends |
| `progress.slow_tool_seconds` | `20` | Tool calls at least this slow are recorded for `dev time` |
| `progress.max_parallel` | `4` | Crew members that may work at once; the guard refuses a further launch ([The crew](crew.md#guardrails)) |
| `progress.stale_agent_hours` | `6` | A crew member with no report for this long is closed as lost, and its step goes back to the main session |
| `work.mode` | `guided` | `guided`: decisions that are the developer's wait for the developer; `auto`: the crew goes on with the recommended option and records it. A run can set its own (`--mode`) |
| `ui.powerline` | `false` | Draw Nerd Font arrows between the status pills in `dev status` and `dev overview` (needs a Nerd Font in your terminal) |
| `handoff.enforce` | `true` | When a session made commits without updating the handoff note, the Stop hook asks the agent once to update it |
| `canary.enforce` | `true` | When a final message lacks the status block, the Stop hook asks the agent to reply again with it |

### Branch names

| Key | Default | Meaning |
| --- | --- | --- |
| `branches.types` | `feat`, `fix`, `refactor`, `perf`, `docs`, `test`, `build`, `ci`, `chore`, `deps`, `style`, `revert`, `hotfix`, `release`, `integration` | Allowed branch types; your list replaces the default |
| `branches.forbidden_words` | `claude`, `codex`, `copilot`, `cursor`, `chatgpt`, `gpt`, `openai`, `anthropic`, `gemini`, `agent`, `ai`, `bot`, `worktree`, `tmp`, `temp`, `wip` | Words a branch name may not start with; your list replaces the default |

### Git and GitHub

| Key | Default | Meaning |
| --- | --- | --- |
| `commits.types` | `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`, `deps` | The Conventional Commit types that `dev commit` and PR titles accept; your list replaces the default |
| `gitx.github_hosts` | `[]` | GitHub Enterprise hosts, for example `["github.acme.com"]`, whose repositories `dev pr`, `dev run`, `dev release`, and `dev issue` treat as GitHub |

See [Git and GitHub](git-and-github.md).

### Sync, cleanup, and Docker

| Key | Default | Meaning |
| --- | --- | --- |
| `sync.push` | `true` | Push the handoff repository after each sync and each work-item change |
| `sync.interval_minutes` | `60` | The sync interval that `dev setup` asks for. The `sync` job's schedule is `jobs.sync`; change it with `dev jobs set sync --every 30m` |
| `gc.auto` | `true` | The nightly `gc` job applies the safe cleanup; `false` (the wizard's "report only") keeps the job off |
| `gc.grace_hours` | `24` | Idle time before a merged or fully contained worktree is removed |
| `gc.idle_artefact_days` | `7` | Idle time before regenerable build output is pruned in a worktree |
| `gc.temp_days` | `7` | Age after which temp entries are purged |
| `gc.docker` | `true` | Also prune dangling Docker images and build cache older than 7 days |
| `docker.cpu` | `6` | Colima VM CPUs |
| `docker.memory` | `12` | Colima VM memory in GB |
| `docker.disk` | `80` | Colima VM disk in GB (sparse; it can only grow) |
| `docker.vm_type` | `vz` | Apple Virtualization framework |
| `docker.rosetta` | `true` | Rosetta for amd64 images (with `vz`) |
| `docker.mount_type` | `virtiofs` | Host folder sharing |
| `docker.autostart` | `false` | `true` registers Colima with `brew services` so it starts at login (same as `dev docker autostart-on`) |

### Jobs and audit

| Key | Default | Meaning |
| --- | --- | --- |
| `jobs.<name>` | the built-in jobs | Changes a scheduled job (`every` in minutes, `at`, `weekly`, `timeout`, `notify`, `enabled`, `about`, `run_at_load`), or adds your own with `cmd`. `dev jobs set`, `enable`, and `disable` write these keys for you. See [Scheduled jobs and the audit trail](jobs-and-audit.md#add-your-own-job) |
| `audit.publish` | `true` | The `audit` job publishes this machine's audit trail to `audit/<user>@<host>/` in the handoff repository; `false` keeps it local |

```json
{
  "jobs": {
    "sync": { "every": 30 },
    "doctor": { "weekly": "fri 08:30" },
    "lessons": { "enabled": false }
  },
  "audit": { "publish": true }
}
```

Run `dev jobs install` after editing these keys by hand.

### For another developer

Nothing needs editing: `dev setup` detects their login and proposes `<their-login>/handoff`. To share a team handoff
repository, set `handoff_repo` before they install.

## Personal agent rules

`~/.config/devkit/AGENTS.personal.md` is appended, under "Personal additions," to the generated
`~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`. Use it for preferences that are yours only (the language of replies,
editor habits). Shared rules belong in `agents/AGENTS.md` in the devkit repository.

```bash
$EDITOR ~/.config/devkit/AGENTS.personal.md
dev install --only instructions
```

Never edit the generated files directly; the next install overwrites them, and `dev config repair` restores them.

## Environment variables

| Variable | Effect |
| --- | --- |
| `DEV_ROOT` | Overrides `dev_root` |
| `DEVKIT_CONFIG` | Path of the config file (default `~/.config/devkit/config.json`) |
| `DEVKIT_GUARD=off` | In the environment of the agent process (`DEVKIT_GUARD=off claude`), turns the guard off for that whole session (see [Guard rules](guard.md#escape-hatches)) |
| `DEVKIT_LOCAL_CI_PASSED=1` | Prefix of a `git push`: says the full local suite just passed, lifting the push budget for that push. `dev push`, `dev pr create`, and `dev release` accept it like `--ci-passed` |
| `DEVKIT_FLAKY=1` | Prefix of `gh run rerun`: says the failure is a proven flaky test (prefer `dev run rerun <id> --flaky "…"`) |
| `DEVKIT_RAW_GH=1` | Prefix of `gh pr merge` or `gh release create`: runs gh's own command instead of `dev pr merge` or `dev release`; `--admin` stays refused |
| `DEVKIT_PIPE_OK=1` | In a command that commits, pushes, or tags after a pipe: says the pipe's last command is the real check (prefer `set -o pipefail`) |
| `DEVKIT_CROSS_PROJECT=1` | Prefix of `dev task`, `dev note`, or `dev handoff`, or in the agent's environment: lets a project session change another project's records when you asked for it |
| `DEVKIT_SKIP_SECRET_SCAN=1` | In the agent's environment: the guard does not run gitleaks before `git commit` |
| `DEVKIT_RAW_FS=1` | Prefix of `rm`, `find -delete`, `xargs rm`, `git clean -f`, or an overwriting `cp` or `mv`: lets the command delete or overwrite directly instead of through `dev rm`, `dev mv`, or `dev cp` |
| `DEVKIT_TAG_ANY=1` | Prefix of `git tag`: allows a tag that breaks the recorded delivery policy |
| `GITLAB_TOKEN` | `dev ci status` for GitLab when `glab` is not installed (`GITLAB_PRIVATE_TOKEN` works too) |
| `BITBUCKET_TOKEN`, or `BITBUCKET_USERNAME` and `BITBUCKET_APP_PASSWORD` | `dev ci status` for Bitbucket Pipelines, and the Bitbucket sign-in check of `dev doctor` |
| `AZURE_DEVOPS_EXT_PAT` | `dev ci status` for Azure Pipelines and the `az devops` CLI (`AZURE_DEVOPS_PAT` works too for `dev ci status`) |
| `DEVKIT_NO_NOTIFY=1` | No desktop notifications from that process |
| `DEVKIT_THEME=dark\|light` | The wordmark's ink: paper on dark, charcoal on light (otherwise taken from `COLORFGBG`, default dark) |
| `DEVKIT_ALLOW_STUBS=1` | One commit may take unfinished work the developer accepted |
| `DEVKIT_SCOPE_OVERRIDE=1` | In the agent's environment: the main session may edit files a working crew member owns (emergencies) |
| `DEVKIT_FROM_AGENT=<id>` | Set by the hooks on a subagent's `dev progress` and `dev crew` commands, so they write to its lane; never set it yourself |
| `DEVKIT_NO_CI_WATCH=1` | No automatic `dev ci watch` after a push or PR from that process |
| `DEVKIT_SCOPE=all` | Check every turn with the status block and required skills, not only development turns |
| `DEVKIT_NO_AUTOSYNC=1` | No queued background syncs from that process; bulk operations push once at the end |
| `NO_COLOR` | Plain output without colors |
| `DEVKIT_SESSION`, `DEVKIT_AGENT` | Set by devkit in Claude Code sessions (and `DEVKIT_AGENT=job` in scheduled jobs) so `dev progress`, the work-item changelog, and the audit trail know the session and agent; do not set them yourself |
| `DEVKIT_JOB` | Set to the job's name while a scheduled job runs |
| `CODEX_HOME` | Codex home (default `~/.codex`) |
| `CLAUDE_CODE_TMPDIR` | Claude Code's temp root; devkit treats it as temp and purges it |

The shell integration and the agents' environments also set `DOCKER_HOST`, `TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE`,
`NG_CLI_ANALYTICS=false`, `NEXT_TELEMETRY_DISABLED=1`, and `SUPERPOWERS_DISABLE_TELEMETRY=1`. Claude Code's
environment also turns off the telemetry and version checks of the security scanners (`TRIVY_DISABLE_TELEMETRY`,
`TRIVY_SKIP_VERSION_CHECK`, `SEMGREP_SEND_METRICS=off`, `SYFT_CHECK_FOR_APP_UPDATE`, `GRYPE_CHECK_FOR_APP_UPDATE`) and
the review prompts of the security plugins (`ENABLE_STOP_REVIEW=0`, `ENABLE_COMMIT_REVIEW=0`,
`CLAUDE_SECURITY_SCAN_TIP=off`).

## What everyone gets: the devkit repository

These files apply to everyone who installs devkit. Change them in a branch of the devkit repository, not on one Mac.

| File | Controls |
| --- | --- |
| `agents/AGENTS.md` | The global instructions for both agents |
| `config/claude/settings.json` | The settings merged into `~/.claude/settings.json`: permission mode, attribution, `env`, hooks, `fastModePerSessionOptIn` |
| `config/claude/plugins.json` | Claude Code marketplaces, plugins (`plugins`, `disabled`) and user-scope MCP servers |
| `config/codex/config.json` | Codex settings written through Codex's config API (`settings` replaced, `upsert` merged), the `profiles` and the agent roles (`agents`) |
| `config/codex/hooks.json` | Codex hooks |
| `config/codex/plugins.json` | Codex marketplaces, plugins, and MCP servers |
| `config/skills.json` | Third-party skills: repository, pinned commit (`ref`), license, folders, optional `group`, and optional `agents` (`["codex"]` links a skill for one agent only, for example one the other agent's plugin already brings) |
| `config/skill-rules.json` | Which skills each kind of work requires |
| `config/docs/` | Default markdownlint and Vale rules for `dev docs check` |
| `config/git/` | Shared git defaults and the global ignore file |
| `plugin/` | The devkit plugin: its skills, slash commands and the `scout` agent |
| `templates/` | Plan, task, pull request and handoff templates (see [Templates](templates.md)) |
| `Brewfile`, `config/npm-globals.txt` | Packages in `# @group` sections: the base groups (`core`, `docker`, `agents`, `docs`, `security`), one per stack, and one per git host or cloud (`gitlab`, `azure-devops`, `aws`, `azure`, `gcp`, `digitalocean`, `kubernetes`, `terraform`) |

Placeholders `{{DEVKIT}}`, `{{HOME}}`, `{{DEV}}`, and `{{UID}}` are expanded at install time. After editing:

```bash
dev install --only skills,claude,codex   # or the step you changed
dev doctor
```

To update a third-party skill, review the upstream change, put the new full commit SHA in `ref` and run
`dev install --only skills`. Fetched sources are cached per commit in `~/.local/share/devkit/skills-src/`.

See also [Agents](agents.md), [Skills and plugins](skills-and-plugins.md), and [Models and tokens](models-and-tokens.md).
