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
(`>`, `>>`) are checked as writes. The guard also reads what the shell runs inside a command: `$(…)` and backtick
substitutions, `sh -c` and `eval` strings, `xargs` and `find -exec` commands, and a pager that git or gh starts. Quoted
text a command only carries, such as a `dev crew ask` message, is data and never read as a command. A heredoc body
ends at its tag line, and also at a line that starts with the tag and `)`: macOS `/bin/bash` 3.2 ends a heredoc there
inside `$(…)` and runs what follows, while zsh reads on, so the guard reads it the way that shows what bash runs.

## What the guard is and is not

The guard is a seatbelt. It catches an agent's mistakes and the common forms of prompt injection: a command that
deletes your work, pushes or merges without approval, switches devkit off, or starts a session without its hooks. It
reads the command text and the commands the shell, a pager, or an interpreter's string literals hand to a process.

It is not a sandbox. A determined program can still find a form the guard does not recognize, for example a command
it assembles at run time from pieces the guard cannot read, or a file it unpacks and git later runs. Each such form
the red team finds becomes a rule here and a test, but the list can never be complete.

The lasting boundary is credentials: what an agent's tools can reach is limited by the tokens and keys they hold.
Separating the agents' credentials from yours is planned for devkit 26.11.4. Until then, treat the guard as the net
under your own review, not as a replacement for it.

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
| Dotfiles directly in `~` (`~/.zshrc`, `~/.zprofile`) | allow |
| A repository's git folder: the `.git` folder or file itself, and its `config`, `config.worktree`, `hooks/`, `info/`, and `commondir` (also of a submodule under `.git/modules/` or a linked worktree under `.git/worktrees/`), and git's own configuration (`~/.gitconfig`, `~/.config/git/config`), by the Write and Edit tools, `apply_patch`, redirections, `cp`, `tee`, and `ln` | deny: git runs what they name (a pager, fsmonitor, an ssh command, a hook) under later ordinary git calls. Use `.gitignore` for ignores; ask the developer for anything else |
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
| A file under `crew/` | deny → `dev crew set <member> <field> "…" --why "…"`, `dev crew raci <work> --set "…" --why "…"` |
| A file under `lessons/` | deny → `dev lesson add\|recur\|prevent\|retire …` |
| A file under `audit/` | deny → each machine's `audit` job publishes its trail; read it with `dev audit` |
| A Write, Edit or `apply_patch` that adds, changes or removes the `embargo:` list or the `backup_policy:` of a project's note (including deleting or moving the note, and an edit the guard cannot apply to the note as it is) | deny → `dev branch embargo <branch> --why "…"`; the lift and the policy are [the developer's](#the-developers-own-decisions) |
| A shell write to a project's note or to a file under `policies/`, `crew/`, `lessons/` or `audit/`: a redirect, `sed -i`, `perl -pi`, `tee`, `cp`, `mv`, `rm`, `dev rm`, a program that writes and names one of them, or a call into devkit's code that writes the embargo or the policy | deny → the Edit or Write tool for a note's other sections, the `dev` command for a record |
| `git checkout`, `reset`, `restore`, `revert`, `merge`, `pull`, `rebase`, `stash` or another command that rewrites files, run in the handoff repository | deny → `dev sync` commits, pulls, and pushes it |
| `projects/<other project>.md` from a session in another project | deny |
| `plans/<other project>/…` from a session in another project | deny |
| `initiatives/<slug>/…` from a project that is not a member of that initiative | deny → `dev initiative add <slug> <project>` from a portfolio session |
| Anything, from a portfolio-level session (started in `~/Development` or in the handoff repository) | allow, except the preceding rows about devkit's records, the embargo list, the backup policy, shell writes, and git |

The file tools keep every other part of a note open: the guard compares the note's `embargo:` list and `backup_policy:`
before and after the edit, and refuses only a change to them. A shell write cannot be compared, so it is refused for
the whole note. If an embargo still leaves the note another way (a hand edit, a merge, an older note checked out), it
keeps holding: devkit also reads the `git.embargo` records of the audit trail, and an embargo whose last record sets
it holds until `dev branch embargo --lift`.

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
| `git config` that sets `core.pager`, `pager.<command>`, or a key the guard cannot read (`git config $K …`), in any scope or file, also as `git config set` | deny: git runs the pager as a command each time it pages, under every later read. Use `git --no-pager` or `-P` for one call |
| `git config` that sets `core.editor`, `sequence.editor`, `core.fsmonitor`, `core.sshCommand`, `core.hooksPath`, `include.path`, or `includeIf.<condition>.path` (the main session and crew members alike) | deny: their value is a command line, or a file git runs or reads more configuration from, under later calls such as `git status` or `git fetch` |

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
| `gh config set pager …` (or a key the guard cannot read) | deny: gh runs its pager as a command for every later read |

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

### devkit's own state, configuration, and install

devkit keeps runs, sessions, approvals, and the audit trail in `~/.local/state/devkit`, its configuration in
`~/.config/devkit`, and the [pinned copy](getting-started.md#the-pinned-copy) the hooks run in
`~/.local/share/devkit`. A forged record there could approve work nobody approved, so agents may not write them:
devkit's own commands (`dev progress`, `dev task`, `dev crew`, …) write them in their own process. devkit makes the
three folders readable and writable by your account only (`0700`) every time it starts.

| Action of an agent | Decision |
| --- | --- |
| A file write (Write, Edit, `apply_patch`) in those three folders, or in a subagent's meta file | deny, with the `dev` command that owns the record |
| A shell write there: a redirect, `tee`, `cp`, `mv`, `ln`, `rm`, `dev rm`, `sed -i`, an interpreter that writes files, also inside `sh -c`, `eval`, `xargs`, or `find -exec` | deny |
| `dev config set safeguards.*` or `dev config edit`, or a `DEVKIT_*` or `DEV_ROOT` value added to the `env` block of an agent settings file or to a shell startup file such as `~/.zshrc` | deny |
| Replacing, writing, or deleting a `dev` that a shell finds: `~/.local/bin/dev` (the link itself) or another `dev` earlier on `PATH` | deny → the developer runs `dev install --only cli` |
| `HOME`, `DEVKIT_CONFIG`, `DEV_ROOT`, or another `DEVKIT_*` variable set for `dev`, `python -m devkit`, a program that imports devkit, or a nested `claude` or `codex` session; `launchctl setenv` of one of them | deny; the one-command [escape hatches](#escape-hatches) still work |
| `PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONHOME`, `PYTHONUSERBASE`, `PYTHONPLATLIBDIR`, `PYTHONEXECUTABLE`, or `PYTHONBREAKPOINT` set for the same commands | deny: they load other code into devkit's own process. `bin/dev` runs `python3 -I -S`, which ignores them, the current folder, and the user and site-packages folders (and their `.pth` files); the rule also covers `python3 bin/dev`. A repository's own tests may still set `PYTHONPATH` |
| `dev install`, `dev update`, or `install.sh` | deny: they change what every hook and the guard run, so the developer runs them, in a terminal |
| `dev config lock` or `dev config unlock` | deny: they add or remove the [settings lock](agents.md#the-settings-lock) that keeps settings files from turning off devkit's hooks |
| `dev hook <event>`, in any shell form (a pipe, `sudo`, `bash -c`, `eval`, `find -exec`, `python3 bin/dev`) | deny, for every session: only the harness runs a hook, when its event fires. A hook event typed through a shell could forge your prompt or answer |
| A write, move, or delete of a Claude Code transcript (`~/.claude/projects/**/*.jsonl`, a subagent's too) or a Codex session file (`~/.codex/sessions`, `archived_sessions`), with a file tool, a patch, or the shell | deny: devkit reads your approvals from them. Claude Code's memory files stay writable |
| A program an agent runs (inline, from its input or a pipe, or a script outside the workspace) that loads a devkit module and reaches a function that records or uses your prompts, answers, and grants, or `devkit.state`'s writers | deny, for every session. The guard also reads the agent's own files the program loads (`sys.path` plus `import`, `python -m`, `runpy`, `exec(open(…).read())`, `SourceFileLoader`), three levels deep, and refuses code the program decodes and runs at run time. A repository's own scripts and tests are not judged by this rule |

devkit checks this a second time when it starts: it takes a `DEVKIT_CONFIG` other than
`~/.config/devkit/config.json`, or a `DEV_ROOT` other than the configured `dev_root`, only from your terminal, with no
agent among its parent processes. Otherwise it warns on stderr, uses the standard values, and drops the variable for
the processes it starts. Reading these files is always fine. Only the developer changes safeguards, in a terminal.

### Nested agent sessions

An agent may start another `claude` or `codex` session, which runs with devkit's hooks like any other. A session
started without them would have no guard, so these are denied to every agent:

| Command | Why |
| --- | --- |
| `claude --settings …`, `--setting-sources …` | They load other settings, or leave out the ones with devkit's hooks |
| `claude --bare` | It skips every hook |
| `claude --dangerously-*`, `--allow-dangerously-*`, or `--permission-mode bypassPermissions` | They drop the permission checks |
| `codex -c` or `--config` on a key that names hooks, the sandbox, approvals, or `features`, or a key the guard cannot read | They rewrite the hooks, the sandbox, or the approval policy |
| `codex --sandbox danger-full-access`, `--dangerously-*`, `--yolo`, or `--disable <feature>` | They turn the sandbox or a feature off |
| `codex -p <profile>` or `--profile <profile>` | A profile in `config.toml` can set the same sandbox, approval, and hook keys as `-c`. Start a profiled session yourself, in a terminal |
| `CLAUDE_CONFIG_DIR` or `CODEX_HOME` set for either | They point the session at another settings folder |

These rules also apply when a package runner starts the agent (`npx @anthropic-ai/claude-code`, `npx claude-code`,
`bunx`, `pnpm dlx`, `yarn dlx`, `@openai/codex`), and when interpreter code hands the command to a process
(`subprocess.run(['claude', '--bare', …])`, `os.system('claude --bare …')`, `child_process.spawn('dev', ['hook',
…])`). The same goes for `dev hook <event>`, which no agent tool call runs.

A nested session with your own settings still runs, and so does a prompt that only names these flags. If the work
needs another configuration, start that session yourself in a terminal.

### Commands the guard cannot name

The [irreversible-action gate](crew.md#guardrails) classifies each command by the program it runs and the command it
gives it. When the shell names the program only when it runs, the guard cannot know what it does, so the command is the
action `unknown`, tier 2, with the target `cmd-<hash>` of that exact command text. For example:

- `$(printf gh) pr merge 12`, `${X:-gh} pr merge 12`, `g=gh; $g pr merge 12`, or `env -S 'gh pr merge 12'`;
- `gh $SUB merge 12` or `gh pr $ACT 12`, where a variable names gh's command, and `git $SUB push`, where a variable
  names git's;
- a program that builds the name of what it calls, such as `getattr(__import__('os'), 'sys' + 'tem')`, or starts a
  command that begins with what it fills in at run time, such as ``execSync(`${tool} pr merge 12`)`` or
  `subprocess.run(f"{tool} …")`.

These are not `unknown`:

- a known program in a folder a variable names, such as `$VENV/bin/python` or `"$VIRTUAL_ENV/bin/pytest"`, unless
  the same command sets that variable or `IFS`;
- text inside an interpreter's string literals: `${…}`, `{…}`, `[…]` and `*` there belong to the language (a
  template literal, an f-string, a list), and the guard looks for spawn calls in the program's code, not in the
  text it carries. A program named in such a string (`os.system('gh pr merge 12')`) is still classified.

A pager is a command too: `git -c core.pager=…`, `-c pager.<command>=…`, `GIT_PAGER`, and `PAGER` for git, and
`GH_PAGER` and `PAGER` for gh, are classified as the command they name, and so are `core.editor`,
`sequence.editor`, `core.fsmonitor`, and `core.sshCommand` set with `-c` or a `GIT_CONFIG_KEY_<n>` pair on the
command. A git call gets configuration from a source the guard cannot read, and is `unknown`, when it uses
`--config-env` for one of these keys, sets `core.hooksPath` or an include with `-c`, or runs with `GIT_CONFIG_GLOBAL`
or `GIT_CONFIG_SYSTEM` (other than `/dev/null`), `GIT_CONFIG_PARAMETERS`, or a `GIT_CONFIG_COUNT` whose pairs are not
all on the command. A crew member may not set these variables at all.

A crew member never runs tier 2, even with a grant. In the main session, while `safeguards.irreversible_main` enforces,
the refusal names the command's target. Write the program's name out as a plain word when you can. If it must run as
it is, type this line on its own in your prompt:

```text
grant unknown cmd-<hash>
```

The grant opens that one command text, once, in the repository of the session you typed it in, and expires after 30
minutes. `unknown` is never
pre-approved with `dev crew mode auto --approve`.

### The developer's own decisions

Lifting an embargo and publishing a branch make its commits public, and the backup policy decides whether devkit
publishes a project's branches at all. An embargo keeps a branch off the remote, so pushing an embargoed branch is
the developer's decision too. These commands are the action `developer-only`, tier 2:

| Command | Grant line that opens it once |
| --- | --- |
| `dev branch embargo --lift <branch>` | `grant embargo <branch>` |
| `git push`, `dev push`, `dev pr create` or `gh pr create` that sends an embargoed branch, or commits its embargo holds (under another name, a commit id, a branch made from it, a tag, `--all`) | `grant embargo <branch>` |
| `git push`, `dev push`, `dev pr create` or `gh pr create` of any branch while devkit cannot read the project's handoff note | `grant embargo <branch>` |
| `dev backup --public` | `grant backup <project>` |
| `dev backup policy on\|off\|ask` | `grant backup <project>` |

The developer can still push an embargoed branch in a terminal. `dev backup` and `dev sync` hold embargoes
themselves, so they need no grant for it. A crew member never runs them. In the main session they are refused without a grant in every setting of
`safeguards.irreversible_main`, the first week's audit included: that week measures the gate's guesses, and these are
exact commands. A command that names one of them and that the guard cannot parse or check is refused too, and so is
a push or a `pr create` the guard cannot parse in a project with an embargo or a note devkit cannot read. Everything
else keeps the audit week.

Type the grant line on its own in your prompt, for example:

```text
grant embargo fix/token-leak
grant backup acme/api
```

A grant opens one of these commands, for that branch or project only, in the repository of the session you typed it
in, and expires after 30 minutes. `grant backup <project>` opens one public backup or one policy change, not both.
`grant developer-only <target>` with the target the refusal names (`embargo/<branch>`, `backup-public/<project>`,
`backup-policy/<project>`) works too. None of them is ever pre-approved with `dev crew mode auto --approve`. A dry run,
the help, `dev branch embargo` (the list), and `dev backup policy` (the read) need no grant.

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
| A crew member runs `dev progress …` or `dev crew ask\|answer\|…` | routed to its own lane when the shell runs only that one command; otherwise refused, since it would act as Morpheus |

A message is data, so quoted operators in it (`dev crew ask "x; y && z"`) keep the call a single command. A `$(…)` or
backtick that the shell expands in a message runs a command of its own, so it makes the call more than one command.

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
- Most exceptions in the guard are logged to `~/.local/state/devkit/logs/hooks.log` and the call is allowed. These
  checks fail closed instead, because letting the call through would defeat them: a crew member's shell command that
  the lane check cannot read or judge; an irreversible action, in enforce, when the gate itself fails and the text
  looks irreversible; the MCP gate; and the developer-only actions (lifting an embargo, `dev backup --public`,
  `dev backup policy`).
- Every deny and ask is logged there too, and written to the audit trail (`dev audit --action guard`), so you can
  review what was blocked.

The guard reads the command text and the commands the shell runs inside it, but not what a program decides to run at
run time. Such a command is the `unknown` action (see
[Commands the guard cannot name](#commands-the-guard-cannot-name)).

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

The rules are pure functions covered by `tests/test_devkit.py`, and the bypasses found by red-team testing by
`tests/test_red_team.py` and `tests/test_guard_refusals.py`:

```bash
cd ~/Development/fireflysoftwaresolutions/devkit
python3 -m unittest discover -s tests -t . -v
/usr/bin/python3 -m unittest discover -s tests -t . -v     # the macOS system Python (3.9)
```

See also [Delivery](delivery.md), [Teams and machines](teams.md) and [Agents › Hooks](agents.md#hooks).
