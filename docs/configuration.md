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
| `stacks` | `["java", "python", "php", "node"]` | The stacks whose packages `dev setup` and `dev install --only tools` install, and whose tools and language servers `dev doctor` requires. `dev doctor` lists the other stacks as not chosen, not as failures |
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
| `skills.optional` | `{"iac": ["terraform"]}` | Skill groups the setup wizard offers, but never installs by default, each with the tools that bring the offer: when you pick Terraform, the wizard offers `iac` and adds it to `skills.groups` unless you say no |
| `skills.enforce` | `true` | Enforce the skills each kind of work requires (see [Skills and plugins › Required skills](skills-and-plugins.md#required-skills)) |
| `skills.rules` | `[]` | Your own skill rules. A rule with the same `id` as a devkit rule changes the fields you give; a new `id` adds a rule |

Apply group changes with `dev install --only skills`. Rule changes apply at the next prompt.

A rule triggers on what the work does, never on the words of a prompt: `files` (globs of changed files), `commands`
(command words the agent runs), and `activities` (kinds of work from `dev crew raci`). `content` is a pattern the
matching files must also contain; with `content_files`, files of those globs match too when they contain it.

```json
{
  "skills": {
    "rules": [
      { "id": "ui", "skills": { "claude": ["frontend-design"], "codex": ["impeccable", "frontend-design"] } },
      { "id": "terraform", "why": "Terraform style and tests", "files": ["*.tf"], "activities": ["infrastructure"],
        "skills": { "claude": ["terraform-style-guide"], "codex": ["terraform-style-guide"] } }
    ]
  }
}
```

### Extensions

| Key | Default | Meaning |
| --- | --- | --- |
| `ext.add` | `[]` | Your own plugins, skills, and MCP servers, on top of devkit's |
| `ext.remove` | `[]` | Ids of devkit's extensions you do not want installed |
| `ext.disable` | `[]` | Ids to keep installed but off |
| `ext.enable` | `[]` | Ids devkit ships off that you turn on |

Change them with `dev ext add|remove|enable|disable` (see
[Commands › dev ext add](commands.md#dev-ext-add--remove--enable--disable)), which checks each entry and applies it,
rather than by hand; `dev ext ls` shows the result with its layer. Ids are
those `dev ext ls --json` prints. devkit itself and its security plugins (`security-guidance`, `claude-security`,
`codex-security`) stay on whatever the overlay says. `dev install`, `dev config repair`, and the managed-settings
lock use the same effective set, so they never undo your choice.

```json
{
  "ext": {
    "add": [
      { "kind": "mcp", "name": "postgres", "agents": ["codex"], "command": "npx", "args": ["-y", "pg-mcp@2.1.0"],
        "license": "MIT", "why": "Postgres tools" },
      { "kind": "skill", "name": "k6", "repo": "grafana/skills", "ref": "<40-char sha>", "path": "skills/k6",
        "license": "Apache-2.0", "why": "load tests" }
    ],
    "disable": ["plugin:claude:commit-commands@claude-plugins-official"]
  }
}
```

### Work, progress, and display

| Key | Default | Meaning |
| --- | --- | --- |
| `work.lock_hours` | `8` | How long a claimed task stays locked without renewal (see [Teams and machines](teams.md#locks-across-machines)) |
| `progress.nudge_minutes` | `8` | Minutes without an update to the developer before the hooks remind the agent |
| `progress.notify` | `true` | Desktop notifications when a run blocks or finishes, and when a long turn ends. Security notices, such as an extension accepted, show even when it is `false` |
| `progress.notify_after_minutes` | `10` | A turn at least this long sends a notification when it ends |
| `progress.slow_tool_seconds` | `20` | Tool calls at least this slow are recorded for `dev time` |
| `progress.max_parallel` | `4` | The crew's slots: how many crew members may work at once (1 to 10), counting the helpers they launch; the guard refuses a launch that does not fit ([The crew](crew.md#guardrails)). The team's `policy/progress.json` in the handoff repository may set a lower ceiling, such as `{"max_parallel": 3}` |
| `progress.quota_steps` | `{"50": 1.0, "70": 0.75, "85": 0.5}` | How the plan's recorded usage lowers the slots: from each usage percent, this share of `max_parallel`, rounded down (never below one). The 5-hour and the weekly windows both count, and the lower result wins; an expired reading counts for nothing. Setting a step adds it or changes its share; a share of `1` turns a step off. With 6 slots at 87 %, 3 members work at once |
| `progress.quota_percent` | `90` | From this usage of the 5-hour or the weekly window, one crew member works at a time, until the window resets |
| `progress.slot_weights` | `{"builder": 1.0, "review": 0.5, "read": 0.5, "scout": 0.0}` | What one launch costs, by kind (0 to 1): a review-only or read-only member (its tool level, or its parent's for a helper), the Lookup scout (Sparks), or a builder (every other agent). The costs in use must fit the slots, so two reviewers fit in one builder's slot and the Lookup scout (Sparks) always runs |
| `progress.reserve` | `1` | Slots kept for the developer's own request while the crew works: queued plan steps from an earlier turn do not take them; a launch for what the developer asked in the current turn does (audited as `crew.reserve`). Never the last slot of a limit of one |
| `progress.max_depth` | `2` | How many levels of agents may run below the main session (1 to 5): crew members, and the helpers they launch. A deeper launch is refused |
| `progress.stale_agent_hours` | `6` | A crew member with no report for this long is closed as lost, and its step goes back to the main session |
| `progress.stale_agent_minutes` | `90` | A working crew member that has been silent this long (no tool call, no step, no note) is marked lost in the run; from 5 to 1440 |
| `progress.stale_agent_minutes` | `90` | The reconciler's limit: a working crew member silent this long (no hook, no update) is lost and frees its slot, and its step waits for a new owner. It works again, with its step, at its next hook. A member stopped with TaskStop frees its slot at once, and its step waits unverified |
| `work.mode` | `guided` | `guided`: decisions that are the developer's wait for the developer; `auto`: the crew goes on with the recommended option and records it. A run can set its own (`--mode`) |
| `crew.gates.enforce` | `true` | At SubagentStop, the crew gates send a member back once for each proof, skill, unfinished line, test that cannot fail, or lesson it still owes; `false` only records them in the audit trail (see [The crew › Every report is checked](crew.md#every-report-is-checked)) |
| `crew.route_hint` | `true` | The prompt hook reminds the tech lead to declare each request's kinds of work by their ids from the catalog and route each by the RACI, in any language; `false` leaves it out |
| `ui.powerline` | `false` | Draw Nerd Font arrows between the status pills in `dev status` and `dev overview` (needs a Nerd Font in your terminal) |
| `dashboard.idle_minutes` | `30` | The live dashboard server stops after this many minutes with no page open and no request |
| `handoff.enforce` | `true` | When a session made commits without updating the handoff note, the Stop hook asks the agent once to update it |
| `handoff.block_after_commits` | `5` | Below this many commits newer than the handoff note, the reminder before the end of the turn is enough; at it, the Stop hook sends the turn back |
| `handoff.block_after_minutes` | `60` | The Stop hook also sends the turn back when the oldest commit newer than the note is this many minutes old, or after a milestone: the end of a run (`dev progress done`), a work item closed, a push, a PR, or a release. An intermediate step is not a milestone, and a milestone counts only the commits made before it. While crew members work, the main session gets the reminder only, and a crew member is never sent back for the note |
| `canary.enforce` | `true` | When a final message lacks the status block, the Stop hook asks the agent to reply again with it; while a `dev progress` run is active (running or blocked), it also sends back once a final message without the Progress bullet or with other step counts than the run's, with the line to use (see [Progress and communication › The Progress line](progress-and-communication.md#the-progress-line)); at SubagentStop, a crew member without its status block or its `dev progress step` is sent back once |
| `canary.roles` | `true` | When a final message or a crew member's report names a crew member alone instead of `<role> (<Name>)`, such as Backend engineer (Tank), the Stop or SubagentStop hook sends it back once per turn (per member for reports). The role can be in any language; the status block's header and Crew line count, and names in code spans and commands do not. The tech lead (Morpheus) is checked too, so write the main session as the tech lead. Off with `canary.enforce` too |

`dev crew` and `dev crew next` show the slots in use and why the limit is lower ("1 of 3 slots in use (lowered from 6:
5-hour usage at 87 %)"), the status block's Crew line ends with the same, and each change of the effective limit is
audited (`crew.limit`). `dev crew next` lists the ready steps in the order the tech lead launches them: the longest
critical path first, then your latest request, then the oldest; a launch out of that order needs
`dev crew next --first "<step>" --why "…"`. You set any of these keys in a terminal with
`dev config set progress.max_parallel 6`, validated and audited; agents are refused.

### Backups

`dev backup` and the hourly `dev sync` copy branches whose commits exist only on this Mac to the remote, when that
push starts no CI run (see [Git and GitHub › Branches](git-and-github.md#branches)). Each project's backup policy is
not a key here: it lives in the project's handoff note, set with `dev backup policy on|off|ask`.

| Key | Default | Meaning |
| --- | --- | --- |
| `backup.auto` | `true` | `dev sync` backs up the branches that need it, every hour and at the end of a session; `false` leaves backups to `dev backup` |
| `backup.after_minutes` | `30` | `dev sync` backs up only commits older than this (0 to 1,440 minutes), because a younger commit may still be amended; `dev backup` takes them all |

### Background AI

Jobs that run with no agent session, such as `dev sleep` merging lessons written in different languages, can ask a
small model, headless. It is off until you turn it on; `dev setup` asks once. Off, over its daily budget, or failing,
the job compares the lessons' anchors instead.

| Key | Default | Meaning |
| --- | --- | --- |
| `ai.enabled` | `false` | Let background jobs ask a model. Each call runs outside any repository, saves nothing, and has a timeout |
| `ai.agent` | empty | `claude` or `codex`; empty uses the first one installed |
| `ai.model.claude` · `ai.model.codex` | `haiku` · `gpt-6-luna` | The model each agent uses for these calls (`claude -p` with no tools, or `codex exec` with a read-only sandbox) |
| `ai.daily_calls` | `20` | At most this many calls a day |
| `ai.timeout` | `60` | Seconds before a call is given up |
| `ai.asked` | — | Set by `dev setup` once you answered, so it does not ask again |

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
| `delivery.draft_pr` | `false` | When `true`, `dev pr create` opens a draft pull request unless you pass `--ready`; `dev pr ready` marks it ready, which starts the one CI run |

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
| `audit.retention.local_months` | `13` | Months of audit records kept on this machine: a full year plus one month. A value under `floor_months` is raised to it |
| `audit.retention.team_months` | `13` | Months of this machine's records kept in the handoff copy, which more people can read. Pruning removes files from the working copy only; git history keeps every published month. See [The audit trail › Retention](audit.md#retention) |
| `audit.retention.floor_months` | `3` | The least any retention may be, and the hard minimum itself: `dev config validate` refuses 1 or 2. For fewer records, set `audit.level` to `security` instead |
| `audit.hold` | `false` | A legal hold: `true` stops all pruning on this machine and in the handoff copy |
| `audit.level` | `changes` | How much is recorded: `security` (refusals, security and configuration records, overrides), `changes` (everything devkit records), or `all` (also read-only commands). See [The audit trail › Levels](audit.md#levels-how-much-is-recorded) |
| `audit.forward` | `""` | Also sends each new record to an absolute path to a file (JSON lines), `syslog`, or `syslog://host:port` (CEF lines). See [The audit trail › Query and export](audit.md#query-and-export) |
| `audit.redact.extra` | `[]` | More regular expressions for the redactor, for secrets only your team uses |

Only you change `audit.*`, in a terminal: agents are refused `dev config set audit.*`. The team policy,
`policy/audit.json` in the handoff repository, can raise the retention and the level and add redaction patterns for
every machine; it can never lower or remove them. `dev config validate` checks every value; `dev audit config` shows the
effective ones.

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

### Safeguards

Only you change these, in a terminal (`dev config edit`): the guard refuses `dev config set safeguards.*` and
`dev config edit` to every agent.

| Key | Default | Meaning |
| --- | --- | --- |
| `safeguards.readonly_commands` | `[]` | More command prefixes that review and read-only crew members may run, such as `"make lint"`, on top of devkit's read-only allowlist (readers, git and gh reads, devkit's reading commands, and the common checks such as `pytest`, `ruff check`, and `tsc --noEmit`) |
| `safeguards.irreversible_member` | `enforce` | `audit` only records the irreversible actions crew members try instead of refusing them; any other value enforces |
| `safeguards.irreversible_main` | audits for the gate's first week on this Mac, then `enforce` | The same for the main session's gate: its tier-2 actions, and the MCP tools that send or publish outside this machine, need your grant or a verified decision; during the first week they are only recorded (see [Guard rules](guard.md)) |

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
| `DEV_ROOT` | Overrides `dev_root`, only when you set it in your own terminal (see the note after this table) |
| `DEVKIT_CONFIG` | Path of the config file (default `~/.config/devkit/config.json`), only when you set it in your own terminal |
| `DEVKIT_GUARD=off` | In the environment of the agent process (`DEVKIT_GUARD=off claude`), turns the guard off for that whole session (see [Guard rules](guard.md#escape-hatches)) |
| `DEVKIT_LOCAL_CI_PASSED=1` | Prefix of a plain `git push` in the main session: says the full local suite just passed, lifting the push budget for that push. `dev push`, `dev pr create`, and `dev release` do not read it from their environment: pass `--ci-passed` (recorded as attested), or let the evidence of a real `dev ci --run` lift the budget |
| `DEVKIT_FLAKY=1` | Prefix of `gh run rerun`: says the failure is a proven flaky test (prefer `dev run rerun <id> --flaky "…"`) |
| `DEVKIT_RAW_GH=1` | Prefix of `gh pr merge` or `gh release create`: runs gh's own command instead of `dev pr merge` or `dev release`; `--admin` stays refused |
| `DEVKIT_PIPE_OK=1` | In a command that commits, pushes, or tags after a pipe: says the pipe's last command is the real check (prefer `set -o pipefail`) |
| `DEVKIT_CROSS_PROJECT=1` | Prefix of `dev task`, `dev note`, or `dev handoff`, or in the agent's environment: lets a project session change another project's records when you asked for it |
| `DEVKIT_SKIP_SECRET_SCAN=1` | Prefix of a plain `git commit` in the main session: the guard does not run gitleaks before that commit. `dev commit` does not read it from its environment |
| `DEVKIT_RAW_FS=1` | Prefix of `rm`, `find -delete`, `xargs rm`, `git clean -f`, or an overwriting `cp` or `mv`: lets the command delete or overwrite directly instead of through `dev rm`, `dev mv`, or `dev cp` |
| `DEVKIT_TAG_ANY=1` | Prefix of `git tag`: allows a tag that breaks the recorded delivery policy |
| `GITLAB_TOKEN` | `dev ci status` for GitLab when `glab` is not installed (`GITLAB_PRIVATE_TOKEN` works too) |
| `BITBUCKET_TOKEN`, or `BITBUCKET_USERNAME` and `BITBUCKET_APP_PASSWORD` | `dev ci status` for Bitbucket Pipelines, and the Bitbucket sign-in check of `dev doctor` |
| `AZURE_DEVOPS_EXT_PAT` | `dev ci status` for Azure Pipelines and the `az devops` CLI (`AZURE_DEVOPS_PAT` works too for `dev ci status`) |
| `DEVKIT_NO_NOTIFY=1` | No progress notifications from that process; security notices, such as an extension accepted, still show |
| `DEVKIT_THEME=dark\|light` | The colors of the banner and the `dev setup` wizard for a dark or a light terminal. Otherwise devkit reads `COLORFGBG`; when neither is set, as in macOS Terminal, it uses colors that read on both light and dark backgrounds |
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

**Who may point devkit elsewhere.** `HOME`, `DEVKIT_CONFIG`, and `DEV_ROOT` decide which state, configuration, and
handoff repository devkit reads, so devkit takes a non-standard `DEVKIT_CONFIG` or `DEV_ROOT` only from a command you
run in your own terminal, with no agent among its parent processes. Set by an agent's command or a hook, devkit
ignores it with a warning on stderr and uses `~/.config/devkit/config.json` and the configured `dev_root`. The guard
refuses these variables, and every other `DEVKIT_*` variable but the one-command escape hatches, in an agent's
command (see [Guard rules › devkit's own state, configuration, and
install](guard.md#devkits-own-state-configuration-and-install)).

The shell integration and the agents' environments also set `DOCKER_HOST`, `TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE`,
`NG_CLI_ANALYTICS=false`, `NEXT_TELEMETRY_DISABLED=1`, and `SUPERPOWERS_DISABLE_TELEMETRY=1`. Claude Code's
environment also turns off the telemetry and version checks of the security scanners (`TRIVY_DISABLE_TELEMETRY`,
`TRIVY_SKIP_VERSION_CHECK`, `SEMGREP_SEND_METRICS=off`, `SYFT_CHECK_FOR_APP_UPDATE`, `GRYPE_CHECK_FOR_APP_UPDATE`),
the review prompts of the security plugins (`ENABLE_STOP_REVIEW=0`, `ENABLE_COMMIT_REVIEW=0`,
`CLAUDE_SECURITY_SCAN_TIP=off`), and impeccable's update check and telemetry (`IMPECCABLE_NO_UPDATE_CHECK=1`,
`IMPECCABLE_NO_TELEMETRY=1`).

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
