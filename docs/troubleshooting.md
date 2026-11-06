# Troubleshooting

This page lists where devkit keeps its logs and state, the problems people run into most, and how to reset. Start
with `dev doctor`: every failed check prints the command that fixes it.

## Logs and state

| Path | Content |
| --- | --- |
| `~/.local/state/devkit/logs/hooks.log` | Hook errors (with tracebacks), every guard deny and ask, skill reminders, sessions registered at their first prompt |
| `~/.local/state/devkit/logs/sync.log` | Background syncs (the `sync` job, the session hooks, and the queued near-real-time syncs); rotated at 2 MB |
| `~/.local/state/devkit/logs/jobs/<job>.log` | Each scheduled job's output; `<job>.launchd.log` holds launchd's own messages |
| `~/.local/state/devkit/jobs/journal.jsonl` | Every job run: start, end, exit code, duration, last line (`dev jobs journal`) |
| `~/.local/state/devkit/audit/YYYY-MM.jsonl` | The audit trail of this machine, kept two years (`dev audit`) |
| `~/.local/state/devkit/ci/` | Local CI evidence per repository and commit (30 days) and, in `logs/`, the step logs of `dev ci --run` |
| `~/.local/state/devkit/flaky.jsonl` | CI re-runs with `dev run rerun --flaky` and their reasons |
| `~/.local/state/devkit/handoff-push-failed.json` | Present while the handoff repository cannot be pushed |
| `~/.local/state/devkit/lessons-usage.json`, `lesson-signals.jsonl` | How each lesson was used on this machine, and the corrections queued for `dev lesson scan` |
| `~/.local/state/devkit/logs/config.log` | Settings restored by `dev config repair` and changes refused by ConfigChange |
| `~/.local/state/devkit/canary.jsonl` | Status block results per reply: marker, format score, excerpt of misses (90 days) |
| `~/.local/state/devkit/status/` | The latest status block per project, stored as one line |
| `~/.local/state/devkit/sessions/` | Session records (expire after 7 days) |
| `~/.local/state/devkit/runs/` | `dev progress` runs, one file per session |
| `~/.local/state/devkit/timing/` | Turns, slow commands, subagents, and finished runs, one file per month (`dev time`) |
| `~/.local/state/devkit/trash.jsonl` | Every `dev rm` (and replaced file of `dev mv` and `dev cp`): what went to the Trash, from where, and why |
| `~/.local/state/devkit/cache/ci/` | Recent CI/CD runs and typical durations for `dev ci status` |
| `~/.local/state/devkit/backups/` | Copies of every file devkit rewrote |
| `~/.local/state/devkit/cache/` | GitHub user and organizations, sizes; `prs/` holds PR lists |

```bash
dev jobs                                                  # every scheduled job: loaded, last run, next run
dev jobs show sync                                        # one job's recent runs
launchctl print gui/$UID/com.devkit.job.sync | head -30  # what launchd knows about it
tail -50 ~/.local/state/devkit/logs/sync.log
dev sync                                                  # run a sync in the foreground
```

## Sessions and agents

### Claude Code or Codex does not show the devkit context

Restart the session: instructions and hooks are read at session start. A session that missed SessionStart gets the
context at its first prompt, and `hooks.log` records it. If neither happens, check that `~/.claude/settings.json`
contains devkit's hooks (`dev doctor`, Claude section), that Codex trusts them (next problem), and look for errors in
`hooks.log`. `dev doctor` also reports project settings that switch devkit off.

### Codex hooks are not running

`dev doctor` shows `devkit hooks trusted` failing. Codex skips user hooks until they are trusted, and trust is tied to
the hook's definition.

```bash
dev install --only codex      # trusts devkit's hooks again through Codex's API
```

Or review them in Codex with `/hooks` (CLI) or Settings → Hooks (app). Restart open Codex sessions.

### A devkit skill or the scout agent is missing

Both agents install a snapshot of the devkit plugin. `dev doctor` reports "devkit plugin copy is current" for each;
when it fails, run `dev install --only claude` or `dev install --only codex`, which reinstalls the plugin from your
checkout, then restart the agent.

### The status block is missing or scored as malformed

`dev canary` lists replies that did not start with the `[Firefly> DevKit]` marker; `dev` and `dev overview` show the
format score.

- Many misses from one agent: check that its instruction file exists and is generated (`~/.claude/CLAUDE.md` or
  `~/.codex/AGENTS.md`), then run `dev install --only instructions` and restart its sessions.
- A low format score: the agent writes an older one-line form, or leaves out a bullet. Sessions that loaded
  instructions from before 26.11.0 do this until they restart.
- A repository with a long AGENTS.md can crowd out the global instructions in Codex; keep repository files
  focused.

Only sessions that started after devkit was installed are tracked.

### The agent keeps being sent back at the end of a turn

The Stop hook asks for one more pass, never in a loop, when:

| Reason in the message | Fix |
| --- | --- |
| The final message lacks the status block | The agent replies again with it. `canary.enforce: false` turns this off |
| A plan changed without a recorded reason | `dev plan revise <file> --why "…"` for each plan it names |
| Files changed without a required skill | The agent loads the skill and reviews the change. `skills.enforce: false` turns this off |
| Commits without a handoff update | The agent updates the note with the `handoff` skill. `handoff.enforce: false` turns this off |

### Codex profile models are not available

`dev doctor` reports "profile models available" failing when your account does not offer a profile's model. List the
models with `codex debug models`, set `codex.profiles` in `~/.config/devkit/config.json` (see
[Models and tokens](models-and-tokens.md#configuration-keys)), and run `dev install --only codex`.

### No desktop notifications

Check `progress.notify` in your configuration and that `DEVKIT_NO_NOTIFY` is not set. macOS shows them as coming from
Script Editor; allow its notifications in System Settings → Notifications.

## Tasks and the handoff repository

### A task is claimed by someone who stopped working

The session context and `dev team` show who holds it and until when. Ask them first. A lock that is not renewed
expires after `work.lock_hours` (8) and the hourly sync on any machine releases it; after that anyone can claim it. A
lock that has not expired cannot be released by someone else.

### `dev task start` says the task was claimed on another machine first

Two machines claimed the same item within seconds; the first push won. Pick another task, or talk to the holder.

### A change is refused with "give the reason with --why"

`edit`, `move`, and `cancel` change the scope or the plan, so devkit records why. Add `--why "…"`.

### The handoff repository does not push

`dev` shows "the handoff repository could not be pushed since …" under Needs attention, and `dev audit --action
handoff.push` lists the failed attempts. devkit already rebased on the remote and tried once more, so the cause is
one of these:

```bash
cd ~/Development/.handoff && git status && git log --oneline -3 && git push
gh auth status
```

Check that `handoff_repo` in `~/.config/devkit/config.json` matches the `origin` remote, and that `gh auth setup-git`
has run (`dev install --only git`). If `git status` shows a merge conflict (two people changed the same work item at
the same moment), resolve it in the handoff repository, commit, and run `dev sync`. If `.gitattributes` lacks the
devkit merge rules, run `dev install --only handoff`. The warning disappears with the next push that succeeds.

### A scheduled job fails or does not run

`dev doctor` reports `job <name>` as failing, or you got a notification. Look at the job, then run it in the
foreground:

```bash
dev jobs show gc        # schedule, loaded or not, recent runs with their last line
dev jobs logs gc        # the end of its log
dev jobs run gc         # run it now, with the output in the log
```

`not loaded` means launchd does not have the job: run `dev jobs install`. Exit code `124` means the job ran out of
time; give it more with `"jobs": {"gc": {"timeout": 60}}` and `dev jobs install`. A run recorded as skipped found the
previous run still going. A job that should not run on this Mac can be turned off with `dev jobs disable <name>`.

## Configuration

### `dev doctor` reports a git host or cloud as not signed in

The Hosts and Clouds checks cover the hosts and clouds you chose in `dev setup` (`hosts` and `clouds` in the
configuration). They are local checks, and each failure prints the sign-in command: `glab auth login`, `az login`,
`aws configure sso`, `gcloud auth login`, `doctl auth init`, or the token to export for Bitbucket
(`BITBUCKET_TOKEN`) and Azure DevOps (`AZURE_DEVOPS_EXT_PAT`). A missing CLI is installed by
`dev install --only tools`.

### `dev guard check` says "unrecognized arguments"

Quote a command that has its own options: `dev guard check "git switch -c fix/login"`.

## Files

### `dev rm` refused to delete something

The refusal names the reason and the flag that overrides it, for example `tracked by git … → retry with --tracked`,
and nothing in the batch was deleted. Check that you really mean it, then retry with that flag. When the line says
`no override; ask the user`, there is no flag: the path is a repository, a worktree, a workspace root, or a system
folder, and the message says what to use instead (`dev wt rm`, `dev adopt`, or asking the developer). See
[Commands › dev rm](commands.md#dev-rm-paths-flags--dev-rm---undo-n--dev-rm---list).

### Something was deleted by mistake

```bash
dev rm --list        # recent deletions, newest first
dev rm --undo        # restore the last run; --undo 3 restores the last three
```

A restored file that was tracked by git is unstaged again. A path that exists again is not overwritten; the message
names the copy in the Trash. Files you emptied from the Trash cannot be restored.

### The guard blocks `rm`, `find -delete`, or an overwriting `cp`

Outside temp folders and build output, these go through `dev rm`, `dev cp`, and `dev mv`, which are recoverable. Use
them, or prefix the command with `DEVKIT_RAW_FS=1` when a permanent delete is what you want (see
[Guard rules › Deleting and overwriting files](guard.md#deleting-and-overwriting-files)).

## Delivery

### `git tag` is refused by the delivery policy

The project's recorded policy says how its tags look, and the message gives the tag to use (`dev calver --next` for
CalVer). Tag with that, change the policy with `dev delivery apply --set versioning=… --why "…"`, or prefix the
command with `DEVKIT_TAG_ANY=1` for a one-off tag.

### `dev ci status` cannot read the pipelines

It prints the reason instead of the runs:

| Host | Fix |
| --- | --- |
| GitHub Actions | `gh auth status`; `dev install --only git` signs `gh` in to git |
| GitLab CI | Install `glab` and run `glab auth login`, or export `GITLAB_TOKEN` |
| Bitbucket Pipelines | Export `BITBUCKET_TOKEN` (an access token with `pipeline:read`), or `BITBUCKET_USERNAME` and `BITBUCKET_APP_PASSWORD` |
| Azure Pipelines | Export `AZURE_DEVOPS_EXT_PAT` (a token with the Build read scope) |

The message `no CI provider for this remote` means the `origin` remote is on a host that `dev ci status` does not read. The
GitLab, Bitbucket, and Azure readers are tested against recorded API responses; report a host that answers
differently.

### `dev push` says there is no local CI evidence

`dev push`, `dev pr create`, and `dev release` accept a commit only when the files it contains passed the local
suite. The message says when the last run was and which files changed since; run `dev ci --run` again and commit
exactly what it tested. When a check cannot run on your Mac, `dev ci --run` says so: run it yourself and record it with
`dev ci --mark-passed --note "<command>: <result>"`. If the suite passed in this session in another way, pass
`--ci-passed`.

### `dev pr create` says a section is empty

"What and why" and "How to verify" need text you wrote. Write the pre-filled body to a file with
`dev pr create --dry-run | sed '1,/^---$/d' > body.md`, fill those sections, and run
`dev pr create --body-file body.md`; or fill them in your editor with `dev pr create --edit`; or open a draft with
`--draft`.

### The guard refuses `gh pr merge`, `gh release create`, or `git stash`

Use `dev pr merge`, `dev release`, and `dev commit --wip`: they do the same with the checks devkit requires. When you
really need gh's own command, prefix it with `DEVKIT_RAW_GH=1`; `gh pr merge --admin` is never allowed.

## Workspace

### The guard blocked something legitimate

Read the reason in the agent's output or in `hooks.log`. Most denials describe the intended way (`dev wt new`,
`dev clone`, `--force-with-lease`, `dev task …`, `DEVKIT_LOCAL_CI_PASSED=1`, `DEVKIT_FLAKY=1`). For a genuine false
positive, run that session with the guard off and report the case, so the rule can be fixed with a test:

```bash
DEVKIT_GUARD=off claude       # or codex; applies to the whole session
```

### `dev adopt` refuses to move a repository

It refuses while any process works inside the repository or its worktrees, and lists them. Close those Claude Code,
Claude Desktop, Codex, terminal, and editor sessions, then run it again.

### Claude Desktop or Codex app conversations after `dev adopt`

Conversations opened at the old path do not resume there. Start new ones from the new path. In the terminal,
`claude --resume` works from the new path because devkit moved the history folder.

### An LSP plugin does nothing

The Claude Code LSP plugins need their language servers: `typescript-language-server` and `typescript`, `pyright`,
`jdtls` (which needs a Java 25 runtime) and `intelephense`. `dev doctor` checks that they are installed, and
`dev setup` installs them for your stacks.

TypeScript 7 (the native port) ships no `tsserver.js`; `typescript-language-server` then falls back to another
TypeScript install, so diagnostics may not match a TypeScript 7 project.

### Docker commands fail

```bash
dev docker status          # is the VM running?
dev docker up
docker context ls          # colima should be current
echo $DOCKER_HOST          # unix://…/.colima/default/docker.sock (open a new terminal after install)
```

## Resetting

- Apply everything again: `dev install` (idempotent), or `dev setup` to change your choices.
- Restore one file: copy it back from `~/.local/state/devkit/backups/<name>.<timestamp>`.
- Fetch skills again: delete `~/.local/share/devkit/` and run `dev install --only skills`.
- Clear caches: delete `~/.local/state/devkit/cache/`.
- Stop a scheduled job: `dev jobs disable <job>` (or `launchctl bootout gui/$UID/com.devkit.job.<job>` until the next
  `dev install`).

See also [Getting started › What devkit touches](getting-started.md#what-devkit-touches-and-how-to-undo-it),
[Agents](agents.md), and [Teams and machines](teams.md).
