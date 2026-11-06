# Guard rules

The guard is the PreToolUse hook shared by Claude Code and Codex (`lib/devkit/guard.py`). Both agents run without
permission prompts, so the guard is the safety net for the workspace rules. It is not a security boundary: it stops
the accidents that waste time and disk, keeps the handoff repository consistent, and keeps devkit switched on. Every
denial tells the agent the right way, so it corrects itself in one step.

It inspects:

- **Claude Code:** `Bash` (`command`), `Write`, `Edit`, `MultiEdit` (`file_path`) and `NotebookEdit` (`notebook_path`).
  The hook also sees the `Agent` and `Task` tools, to time them and to add required skills to subagent briefs (see
  [Skills and plugins › Required skills](skills-and-plugins.md#required-skills)); no guard rule applies to them.
- **Codex:** `Bash` (`command`) and `apply_patch` (the patch's `Add File`, `Update File`, and `Move to` paths).

Shell commands are tokenized: chains (`&&`, `||`, `;`, `|`, new lines) are checked one by one, `cd` and `pushd` change
the directory used to resolve later paths, leading `VAR=value`, `sudo`, `env`, `command`, `nohup`, `time`, `exec`,
`builtin`, `nice`, and `caffeinate` prefixes are skipped, heredoc bodies are treated as data, and output redirections
(`>`, `>>`) are checked as writes.

## Decisions

| Decision | Claude Code | Codex |
| --- | --- | --- |
| allow | no output | no output |
| ask | you are asked to confirm (used only for writes outside the workspace) | not supported by Codex, so it becomes deny |
| deny | the tool call is blocked and the reason goes to the agent | the same |

## Rules

### File writes (Write, Edit, `apply_patch`, and `>` redirections)

| Path | Decision |
| --- | --- |
| Temp: `/tmp`, `/private/tmp`, `/var/folders`, `$TMPDIR`, `$CLAUDE_CODE_TMPDIR`, `~/Development/.tmp` | allow |
| Agent and tool config: `~/.claude`, `~/.codex`, `~/.agents`, `~/.config`, `~/.local`, `~/.cache`, `~/.docker`, `~/.colima`, `~/.lima`, `~/.ssh`, `~/.npm`, `~/.m2`, `~/.gradle`, `~/.composer`, `~/.cargo` | allow |
| Dotfiles directly in `~` (`~/.zshrc`, `~/.gitconfig`) | allow |
| `/dev/…` | allow |
| Inside the dev root, valid layout | allow |
| Inside the dev root, a file directly in `~/Development` | deny |
| Inside the dev root, a top-level folder that is not an owner you belong to, a host folder (contains a dot), `_local` or a dot folder, for example `kit-consulting-pr12` | deny |
| Backup-style names: `*.bak`, `*.orig`, `*.backup`, `*.old`, `*.rej`, `*~`, `*_old.*`, `*-copy.*`, `* copy.*`, `* (2).*` | deny |
| Inside the handoff repository | see [The handoff repository](#the-handoff-repository) |
| Anywhere else (Desktop, Documents, `~/notes.md`, …) | Claude: ask · Codex: deny |

"Owners you belong to" are your GitHub login and organizations (from `gh`, cached for a day) plus folders that already
exist in the dev root.

### The handoff repository

| Write | Decision |
| --- | --- |
| A file under `items/`, `sessions/`, or `presence/` | deny → `dev task add\|start\|done\|edit\|move\|cancel --why …`, `dev handoff --log "…"` |
| `PROJECTS.md`, `TASKS.md`, `ACTIVITY.md`, `registry.json`, or anything under `log/` | deny → `dev sync` generates them; edit the note instead |
| A file under `policies/` | deny → `dev delivery apply --set key=value --why "…"` |
| A file under `lessons/` | deny → `dev lesson add\|recur\|prevent\|retire …` |
| `projects/<other project>.md` from a session in another project | deny |
| `plans/<other project>/…` from a session in another project | deny |
| `initiatives/<slug>/…` from a project that is not a member of that initiative | deny → `dev initiative add <slug> <project>` from a portfolio session |
| Anything, from a portfolio-level session (started in `~/Development` or in the handoff repository) | allow, except the first two rows |

The same isolation applies to `dev` commands run from a project session:

| Command | Decision |
| --- | --- |
| `dev task\|note\|handoff … -p <another project>` | deny |
| `dev task\|note … -i <initiative the project is not in>` | deny |
| `dev initiative new\|add\|rm …` | deny: membership is a portfolio decision |

### Agent settings that switch devkit off

A write to `.claude/settings.json`, `.claude/settings.local.json`, `.codex/config.toml`, or `.codex/hooks.json` (in any
project or in your home folder) is denied when it contains:

| Content | Why |
| --- | --- |
| `"disableAllHooks": true` | It disables every hook, including devkit's context, guard, and canary |
| `"devkit@firefly-devkit": false` | It disables the devkit plugin |
| `hooks = false` or `codex_hooks = false` | It turns Codex hooks off |

If devkit really should be off somewhere, change devkit's own configuration (see [Configuration](configuration.md)).
`dev doctor` reports such settings that already exist under `~/Development`.

### Git

| Command | Decision |
| --- | --- |
| `git worktree add <path>` outside `~/Development/.worktrees` or a temp folder | deny → `dev wt new <branch>` |
| `git clone <url> [dir]` to anything but the canonical path or a temp folder | deny → `dev clone <url>` |
| `git clone <local path or unparseable URL>` outside `_local` or temp | deny |
| A branch name that breaks the [naming rule](#branch-names), in `git checkout -b`, `git switch -c`, `git branch <name>`, `git branch -m`, `git worktree add -b`, or `git push` | deny, with the rename command |
| `git push --force`, `-f`, combined short flags with `f`, `+refspec` | deny → `--force-with-lease` |
| `git push --force-with-lease` to `main` or `master` | deny |
| The third `git push` of the same branch within 60 minutes | deny unless prefixed `DEVKIT_LOCAL_CI_PASSED=1` |
| `git clean` with `-x` or `-X` | deny (it deletes `.env`, local config and caches) |
| `git reset --hard` with uncommitted changes to tracked files | deny |
| `git stash`, `git stash push`, `save`, `pop`, or `apply` (the stash is shared by every worktree) | deny → `dev commit --wip`, then `dev commit --unwip` |
| A commit, push, or tag chained after a pipe without `pipefail` (`npm test \| tail -5 && git commit …`), and the same for `dev commit`, `dev push`, `dev release`, `gh pr create`, `gh pr merge`, and `gh release create` | deny → run the check without the pipe, start with `set -o pipefail;`, or test the output |
| `git commit` when gitleaks finds a secret in what the commit is about to record: the staged changes, plus any `git add` earlier in the same command, or every tracked change with `commit -a` | deny, with the rule, file, and line of each finding |
| `git tag <name>` that breaks the project's recorded delivery policy (a non-CalVer tag in a CalVer project, a non-SemVer tag in a SemVer project) | deny, with the version to use, unless prefixed `DEVKIT_TAG_ANY=1` |
| `git commit`, `tag`, `merge`, or `notes` whose message (inline or `-F` file) contains AI attribution | deny |
| `git init <dir>` that breaks the layout | deny |

AI attribution means any of: `Co-Authored-By: Claude`, `Codex`, `ChatGPT`, `OpenAI`, `Anthropic`, `Copilot`,
`GitHub Copilot`, `Gemini`, or `Cursor`, `noreply@anthropic.com`, `noreply@openai.com`, `copilot@github.com`,
`cursoragent@cursor.com`, `Generated with Claude Code`, `Generated with Codex`, `Generated by Claude` or `Codex`, and
`🤖 Generated with` (case-insensitive). `dev commit`, `dev pr`, and `dev issue` remove these lines, plus
`Generated with` or `by` Copilot, ChatGPT, and Gemini, before they write anything.

The pipe rule exists because the `&&` sees the exit code of the pipe's last command (`tail`), not of the check, so a
failing test still lets the commit run. It was devkit's first [lesson](lessons.md) turned into automation.

A secret false positive is cleared the gitleaks way: a `gitleaks:allow` comment on that line or an entry in
`.gitleaksignore`. If a real secret was ever pushed, rotate it.

### Branch names

Branch names follow `<type>/<ticket>-<topic>`, for example `feat/PROJ-142-invoice-export`, `fix/login-timeout`,
`release/26.11.4` or `integration/payments`.

| Rule | Example that is denied |
| --- | --- |
| The type is one of `feat`, `fix`, `refactor`, `perf`, `docs`, `test`, `build`, `ci`, `chore`, `deps`, `style`, `revert`, `hotfix`, `release`, `integration` | `feature/login` |
| The name does not start with a tool, agent, or throwaway word: `claude`, `codex`, `copilot`, `cursor`, `chatgpt`, `gpt`, `openai`, `anthropic`, `gemini`, `agent`, `ai`, `bot`, `worktree`, `tmp`, `temp`, `wip` | `claude/fix-login` |
| The topic is lowercase kebab-case; an optional ticket key such as `PROJ-142` may be uppercase | `fix/Login` |
| The topic does not end in a random or generated id | `fix/login-ec71c0` |
| At most 60 characters | — |

`main`, `master`, `develop`, `trunk`, `gh-pages`, and tags are always allowed. `dev wt new` and `gh pr create --head`
apply the same rule. Change the lists with `branches.types` and `branches.forbidden_words`.

### GitHub CLI

| Command | Decision |
| --- | --- |
| `gh repo clone <repo> [dir]` to anything but the canonical path or a temp folder | deny |
| `gh run rerun …` | deny unless prefixed `DEVKIT_FLAKY=1` → `dev run rerun <id> --flaky "…"` |
| `gh pr merge …` | deny unless prefixed `DEVKIT_RAW_GH=1` → `dev pr merge` (checks, reviews, conflicts, and the squash message) |
| `gh release create …` | deny unless prefixed `DEVKIT_RAW_GH=1` → `dev release` (the delivery policy, the CHANGELOG section, and the evidence) |
| `gh pr merge --admin` | always deny: it bypasses branch protection and required reviews |
| `gh pr create --head <branch>` with a branch that breaks the naming rule | deny |
| `gh pr …`, `gh issue …`, `gh release …`, `gh api …` containing AI attribution | deny |

### Files and folders from the shell

| Command | Decision |
| --- | --- |
| `rm -r` (`-R`, `-rf`, `--recursive`) of `/`, `~`, the dev root, the handoff repository, `.worktrees`, `_local`, the devkit repository, `~/Library`, `~/Documents`, `~/Desktop`, `~/Downloads`, `~/Pictures`, `~/Movies`, `~/Music`, `~/Applications`, `~/.claude`, `~/.codex`, `~/.agents`, `~/.config`, `~/.ssh`, or `~/.local` | deny |
| `rm -r` of any `.git` folder | deny |
| `rm -r *`, `./*`, `.`, or `..` while in `~`, the dev root or a top-level folder of it | deny |
| `rm -r` of a whole top-level folder of the dev root | deny |
| `rm -r` of a git worktree | deny → `dev wt rm` |
| `rm -r` of a repository checkout | deny |
| `mkdir`, `touch`, and the destination of `cp`, `mv`, `rsync`, or `ditto`, when they break the layout | deny |
| `cp`, `mv`, and the like to a backup-style filename inside the dev root | deny |

### Deleting and overwriting files

Outside scratch areas, permanent deletes and silent overwrites go through `dev rm`, `dev mv`, and `dev cp`, which
use the Trash, keep a manifest for `dev rm --undo`, stage tracked deletions like `git rm`, and refuse open files and
unsaved work (see [Commands › Files](commands.md#files)).

| Command | Decision |
| --- | --- |
| `rm <path>` or `unlink <path>` | deny → `dev rm <path>` |
| `find … -delete`, or `find … -exec rm` | deny → list the matches with `dev find`, then `dev rm` them |
| `xargs rm` | deny → `dev rm` |
| `git clean -f` (without `-n`) when it would delete untracked paths | deny → review with `git clean -n`, then `dev rm --discard <paths>` |
| `cp` or `mv` onto an existing file, without `-n` or `-i` | deny → `dev cp` or `dev mv` (with `--force`, the old version goes to the Trash) |

These stay allowed: build output (`node_modules`, `dist`, `build`, `target`, `.venv`, `__pycache__`, caches and
coverage folders), anything in a temp folder or the scratchpad, `rm` in a temp folder, and paths built from shell
variables the guard cannot see (`$VAR`, `$(…)`). Prefix the command with `DEVKIT_RAW_FS=1` to bypass these rules for
that command; the protected paths in the previous table stay protected.

### Unfinished work in a commit

| Command | Decision |
| --- | --- |
| `git commit` or `dev commit` whose commit would take lines with `TODO`, `FIXME`, "not implemented" code, placeholders, or empty Python bodies that name no task, issue, or reason | deny, with each `file:line`, and how to track planned work: `TODO(T-…)` (see `dev stubs`) |

The guard looks at what the commit would take: the staged changes, plus what a `git add <paths>`, `git add -A`, or
`commit -a` in the same command would stage. A `dev commit --wip` commit is exempt (work in progress is the point).

### The crew

These rules apply while a progress run has crew members working ([The crew](crew.md)).

| Action | Decision |
| --- | --- |
| A fifth crew member launched while four work (`progress.max_parallel`) | deny, with who is working |
| A crew member edits a file outside the scope of its step (`dev progress assign … --scope`) | deny, and how to ask for more |
| The main session edits a file inside a working member's scope | deny, and how to ask that member |
| A crew member records a decision (`dev crew decide`) | refused by `dev crew`: only Morpheus records the developer's answers |

## Example messages

```text
Raw `git worktree add ../repo-pr12` would create an unmanaged worktree at ~/Development/repo-pr12. Use
`dev wt new <branch>` (…), or your harness's own worktree tool. Only create a worktree when the user asked for one
or parallel work really needs it.

Branch `claude/fix-login` is prefixed with a tool/agent or throwaway marker (`claude`). Branch names describe the
change, not who made it: `<type>/<ticket>-<topic>` (…). Rename it: `git branch -m claude/fix-login <type>/<topic>`

`items/acme/api/T-fwk7a2q1x9m.md` is managed by devkit so that versions, locks, history and attribution stay
correct. Use `dev task add|start|done|edit|move|cancel --why …` for work items and `dev handoff --log "…"` for the
session log.

`rm notes.txt` deletes permanently. Use `dev rm <paths>` instead: it moves them to the Trash with a manifest
(`dev rm --undo` restores them), refuses files that are open or hold unsaved work, stages tracked deletions like
`git rm`, and tells you the exact flag when you really mean it. To bypass, prefix the command with DEVKIT_RAW_FS=1.

This commits, pushes or tags after a pipeline (`… | … && git commit`): the `&&` sees the exit code of the pipe's
last command, so a failing check before the pipe still lets it run. Run the check without the pipe, use
`set -o pipefail;` first, or test its output (`out=$(… | tail -1); [ "$out" = OK ] && …`). Lesson: gate on the real
result.

This would be push #3 of `feat/x` in the last hour, and every push runs CI again. Stop the push-and-see loop: read
the failing log (`gh run view --log-failed`), run what CI runs locally (`dev ci` shows the exact commands), fix,
and push once. If the full local suite has just passed in this session, prefix the push with
DEVKIT_LOCAL_CI_PASSED=1.
```

## Fail-open design

- A command the tokenizer cannot parse (for example unbalanced quotes) is allowed; only the attribution check runs on
  its raw text.
- Any exception in the guard is logged to `~/.local/state/devkit/logs/hooks.log` and the call is allowed.
- Every deny and ask is logged there too, and written to the audit trail (`dev audit --action guard`), so you can
  review what was blocked.

The guard sees the command text, not the inner commands of `$( … )` substitutions or the scripts a command runs.

## Escape hatches

| Hatch | Scope | Use it when |
| --- | --- | --- |
| `DEVKIT_LOCAL_CI_PASSED=1 git push …` | one push | The full local suite has just passed in this session |
| `DEVKIT_FLAKY=1 gh run rerun …` | one re-run | The failure is a proven flaky test, recorded in the handoff note |
| `DEVKIT_CROSS_PROJECT=1 dev task …` | one command | You asked the session to change another project's records |
| `DEVKIT_RAW_FS=1 rm …` | one command | A permanent delete or an overwrite is really what you want |
| `DEVKIT_TAG_ANY=1 git tag …` | one command | A tag outside the delivery policy is intended (or change the policy) |
| `DEVKIT_RAW_GH=1 gh pr merge …` (or `gh release create`) | one command | You need gh's own merge or release, outside `dev pr merge` and `dev release`; `--admin` stays refused |
| `DEVKIT_PIPE_OK=1` in a command that commits after a pipe | one command | The pipe's last command really is the check (prefer `set -o pipefail`) |
| `DEVKIT_ALLOW_STUBS=1 git commit …` | one command | The developer accepts the unfinished work this commit takes |
| `DEVKIT_SCOPE_OVERRIDE=1 claude` | the whole session | An emergency fix in a file a crew member owns while it works |
| `DEVKIT_SKIP_SECRET_SCAN=1 claude` | the whole session | gitleaks reports false positives you cannot mark |
| `DEVKIT_GUARD=off claude` (or `codex`) | the whole session | A session genuinely needs the guard off |

Variables that cover a whole session are read from the agent process's environment, so prefixing a single command
inside the session has no effect. Prefer fixing the rule (with a test) when a denial is a false positive.

## Testing

Check one decision by hand, without running anything:

```bash
dev guard check "git push --force"
dev guard check "git switch -c claude/fix-login"
dev guard check "npm test | tail -5 && git commit -m 'fix: totals'"
dev guard check "gh pr merge 12 --admin"
dev guard check --write ~/Development/.handoff/items/acme/api/T-x.md
dev guard check --write ~/Desktop/notes.md --agent codex
```

The rules are pure functions covered by `tests/test_devkit.py`:

```bash
cd ~/Development/fireflysoftwaresolutions/devkit
python3 -m unittest discover -s tests -v
/usr/bin/python3 -m unittest discover -s tests -v     # the macOS system Python (3.9)
```

See also [Delivery](delivery.md), [Teams and machines](teams.md) and [Agents › Hooks](agents.md#hooks).
