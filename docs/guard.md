# Guard rules

The guard is the PreToolUse hook shared by Claude Code and Codex (`lib/devkit/guard.py`). Both agents run without
permission prompts, so the guard is the safety net for the workspace rules. It is not a security boundary: it stops
the accidents that waste time and disk, keeps the handoff repository consistent, and keeps devkit switched on. Every
denial tells the agent the right way, so it corrects itself in one step.

It inspects:

- **Claude Code:** `Bash` (`command`), `Write`, `Edit`, `MultiEdit` (`file_path`) and `NotebookEdit` (`notebook_path`).
  The hook also sees the `Agent` and `Task` tools, to time them and to add required skills to subagent briefs (see
  [Skills and plugins › Required skills](skills-and-plugins.md#required-skills)); no guard rule applies to them.
- **Codex:** `Bash` and `exec_command` (`command`, and `cmd`, which `exec_command` may send instead: a list is read
  word by word, and when both keys are present and differ, each is judged and the call is refused if either is) and
  `apply_patch` (the patch's `Add File`, `Update File`, and `Move to` paths).

Shell commands are tokenized: chains (`&&`, `||`, `;`, `|`, new lines) are checked one by one, `cd` and `pushd` change
the directory used to resolve later paths, leading `VAR=value`, `sudo`, `env`, `command`, `nohup`, `time`, `exec`,
`builtin`, `nice`, and `caffeinate` prefixes are skipped, heredoc bodies are treated as data, and output redirections
(`>`, `>>`) are checked as writes. The guard also reads what the shell runs inside a command: `$(…)` and backtick
substitutions, process substitutions (`<(…)` and `>(…)` outside quotes, and zsh's `=(…)` at the start of a word,
which runs its command the same way and stands for a file that holds its output, so `cat <(gh pr merge 12)` is a
merge and `. =(echo …)` runs what echo writes; a redirect into one, `cmd > >(…)`, writes no file),
`sh -c` and `eval` strings (with the words after a `sh -c` line in place of its `$0`, `$1`, and
`"$@"`, unless the line, read as the shell reads its words once quotes are removed (`s""hift` is `shift`), holds
anything that can bind them again: `shift`, `getopts`, `eval`, `source` or `.`, `set` with operands, zsh's `argv` or
`1=…`, a function or zsh's anonymous function `() { … }`, or a `for` or `select` loop without an `in` list (`for a do`
loops over them). Then they stay variables, and one that names the program is `unknown`, so
`bash -c 'shift; $1 pr merge 12' sh echo gh` is refused; the line is the first word after all the shell's options, as bash, zsh, sh, dash, and ksh read them, so
`bash -c -e 'line'` and `zsh -c -o errexit 'line'` run `line`, while `csh` and `tcsh` take the word right after
`-c`; `bash` and `sh` read a long option with one dash before any short one, so `bash -norc -c 'line'` is
`bash --norc -c 'line'` and `-rcfile f` takes `f`, and they read `+c` as `-c`), `xargs` and `find -exec` commands, and a pager that git or gh starts. The shells it reads this way are
`sh`, `bash`, `zsh`, `dash`, `ksh`, `fish`, `csh`, and `tcsh`, run by name, by path, or by a script's shebang,
and the guard knows a shell or an interpreter by the file it runs as well as by its name: a symbolic link to one
(`./bash-link -c …`), a hard link, or a renamed copy is read as that program too, and a refusal of either reading
stands. A binary run by its path that the guard cannot identify this way is `unknown` when the command hands it
`-c` (alone or in a cluster such as `-lc`), `--command`, a here-string, a heredoc, a redirect, or a pipe, as it may
run that text as a program. It
reads every one of them with POSIX shell quoting, which `csh` and `tcsh` do not follow in every case (a backslash
before a closing double quote). Every rule applies at each of these
levels, so `find . -exec sh -c 'git push --force origin main' \;` is denied as the plain command is. The guard
reads four levels of nesting; a command nested deeper (`sh -c` inside `sh -c` five times) is refused whatever it
runs, and the irreversible-action gate counts it as `unknown`, because the guard cannot judge what it cannot read. Quoted
text a command only carries, such as a `dev crew ask` message, is data and never read as a command. A heredoc body
ends at its tag line, and also at a line that starts with the tag and `)`: macOS `/bin/bash` 3.2 ends a heredoc there
inside `$(…)` and runs what follows, while zsh reads on, so the guard reads it the way that shows what bash runs.
A `<<WORD` inside `${…}`, `$[…]`, or `$((…))` is part of the expansion and opens no heredoc, so in
`echo ${x/<<E/}` the next line is a command. A `#` starts a comment only at the start of a word, as bash and zsh read it: after a blank, a new line, an operator,
or a redirection. A `#` right after the `)` that ends a substitution is part of the word, so in
`echo x$(true)# <(gh pr merge 12)` the merge runs, while in `(echo a)# <(gh pr merge 12)` it is a comment. Where the
shells read a parenthesis differently (bash reads `a=(1 2)#x` as one word, zsh as a comment), the guard takes the
reading that shows more of what runs.

## What the guard is and is not

The guard is a seatbelt. It catches an agent's mistakes and the common forms of prompt injection: a command that
deletes your work, pushes or merges without approval, switches devkit off, or starts a session without its hooks. It
reads the command text and the commands the shell, a pager, or an interpreter's string literals hand to a process.

It is not a sandbox. A determined program can still find a form the guard does not recognize, for example a command
it assembles at run time from pieces the guard cannot read, or a file it unpacks and git later runs. Each such form
the red team finds becomes a rule here and a test, but the list can never be complete.

Agents work with your own credentials: devkit uses the GitHub account you are signed in to with `gh`, and the tokens
and keys on your Mac, with no separate identity for agents: what an agent's tools can reach is what you can reach.
The guard, the grants you type, and the actions only you may run hold the irreversible steps for you; treat them as
the net under your own review, not as a replacement for it.

## Decisions

| Decision | Claude Code | Codex |
| --- | --- | --- |
| allow | no output | no output |
| ask | you are asked to confirm (used only for writes outside the workspace) | not supported by Codex, so it becomes deny |
| deny | the tool call is blocked and the reason goes to the agent | the same |

**Every refusal has an id.** Each place in the guard that refuses has a stable refusal id, which the audit trail
records with the refusal (`refusal_id`, see [The audit trail](audit.md)) and which `dev bug report --guard <refusal id>`
takes when the refusal was wrong. When devkit's [bug registry](bug-registry.md) knows a bug for a refusal, the
refusal adds what the registry serves: up to two workarounds a maintainer confirmed or a team agent found, as data
that never overrides you, or a note that the refusal stands; then a `devkit-bug:` marker line. The verdict itself
does not change.

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
| A path with a NUL character, in a file tool's `file_path` or `notebook_path` or an MCP tool's path | deny: the system reads a path up to its first NUL, so another file would be written, and a later rule would fail on it and let the call through |

A file tool (Write, Edit, MultiEdit, NotebookEdit, `apply_patch`, and an MCP tool's path) hands its path to the system
as it is, so the guard reads it the same way: `$` and a backtick are ordinary characters, and `..` and links resolve as
the system resolves them. `/private/tmp/$X/../../../Users/me/.config/devkit/config.json` is devkit's configuration
once a folder named `$X` exists, and it is refused like the plain path. In a shell command, `$X` stays a variable the
guard cannot know. A leading `~` is your home folder for Claude's file tools, which expand it. Codex's `apply_patch`
and an MCP server may write `<folder>/~/…` instead, so the guard judges their paths both ways and refuses the write
when either reading is refused. A path with a link followed by `..` (`link/../x`) is judged both as the system reads
it (the link's target, then `..`) and as a program that cleans the path first reads it (`x` beside the link): a write
that either reading puts in devkit's state, configuration, or install is refused. `apply_patch` is judged by its patch
under any key Codex sends it in (`input`, `patch`, or `command`), with its lines split as Codex splits them (on a
line feed only, with a carriage return before it dropped). A patch whose `Add File`, `Update File`, `Delete File`, or
`Move to` path holds a control character or a line separator (`\r`, `\x0b`, `\x1c`, `\x85`, U+2028) is refused, so
the guard and Codex always read the same path. A `Bash` or `Monitor` command is always judged
as a shell command, even one that starts like a patch. The rules on the handoff repository compare paths without case, as macOS folders do:
`~/development/.HANDOFF/projects/other/repo.MD` is another project's note.

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
project, in your home folder, or in `CODEX_HOME` when it is set elsewhere) is denied when it contains:

| Content | Why |
| --- | --- |
| `"disableAllHooks": true` | It disables every hook, including devkit's context, guard, and canary |
| `"devkit@firefly-devkit": false` | It disables the devkit plugin |
| `hooks = false` or `codex_hooks = false` | It turns Codex hooks off |

Write, Edit, `apply_patch`, and shell commands follow this one rule. The guard finds the file however a call names it:

- **Any spelling of the path.** Folder and file names count in any case, because macOS reads `.Codex/Config.toml` as
  `.codex/config.toml`. Any path that resolves there counts too: `~`, `$HOME`, `../`, a linked folder, or a `cd`
  earlier in the same command.
- **Every TOML form.** A bare, quoted, or dotted key, an inline table, or a key spelled with a `\u` escape
  (`"codex_hoo\u006bs"`) counts, on every Python devkit runs on: where Python has no TOML reader (Python 3.9, the one
  in `/usr/bin/python3`), the guard reads the keys itself. A `config.toml` that parses as TOML is denied when any of
  its tables sets `hooks` or `codex_hooks` to `false`. A quoted key with an escape that TOML does not have, set to
  `false`, is denied too.
- **Every JSON form.** A `settings.json` or `settings.local.json` that parses as JSON is denied when it sets
  `disableAllHooks` to `true` or `devkit@firefly-devkit` to `false`, also when a key is spelled with an escape
  (`disabl\u0065AllHooks`). When a key appears twice, the last one counts, as Claude Code reads it.

It judges the text each kind of call writes:

- **Write:** the file's text after the write.
- **Edit and MultiEdit:** the guard applies the edits to the file as it is now, as the tools do (in order;
  `replace_all` for every match, otherwise the first), and judges the whole result. An edit whose `old_string` is not
  in the settings file is denied.
- **A Codex patch** (`apply_patch`): each file it adds, updates, or moves, by the file's text after the patch and by
  the lines it adds, at the path the patch names and at its real path. A move onto a settings file brings the text of
  the file it moves. When a hunk does not apply, or only adds lines, its added lines are also parsed as TOML alone and
  after the file's current text.
- **A shell command whose text is visible:** `>`, `>>`, and `<>` with `echo` or `printf` (with their escapes decoded
  as bash, zsh, and `printf` decode them), `tee` and `tee -a`, a heredoc or here-string into `cat` or `tee`, and
  `dd of=` fed from one of these. How the command opens the file decides what is judged: `>` and `tee` replace the
  file and are judged by what they write; `>>`, `tee -a`, and `<>` keep the file's text and are judged together with
  what it already holds, in JSON files as in TOML files. Text split across two commands therefore counts as one:
  `printf '{"disableAllHooks": ' > f`, then `printf 'true}' >> f`, is denied. Two or more writes to the file in one
  command are judged together. Such a write is denied only when the result turns hooks off, and allowed otherwise.
- **A settings file the guard cannot read in full** (over 1 MiB): `>>`, `tee -a`, or `<>` to it, and a patch that
  updates it or moves a file onto it, are denied, because the part the guard cannot read could finish what the write
  adds. `>`, Write, and a patch that adds the file whole are still judged by the whole text they write.
- **A shell command that hides its text** is denied for these files, whatever it writes: `cp`, `mv`, `install`, `ln`,
  `sed -i`, `perl -pi`, `awk -i inplace`, `ed`, `truncate`, `dd` with other options, a variable, `$(…)`, backticks,
  `$'…'`, or an unquoted glob in the text, `exec 3>`, and interpreter code that names a settings file. Among git
  commands: `git checkout`; `git restore` without `--staged` (`-S`), or with an option other than `--staged`,
  `--source` (`-s`), and `--quiet` (`-q`); `git rm` without `--cached`, or with an option other than `--cached`, `-r`,
  `-f`, `-q`, `-n`, their long forms, `--ignore-unmatch`, and `--sparse`; every git command outside the readers (`git archive`, `git bundle`, `git mergetool`, `git config --file`, an alias);
  and a reader that writes a file, such as `git diff --output`, `git log --output`, or `git grep -O`. The guard reads
  git's options as git parses them: an abbreviation (`--wor` for `--worktree`), a value attached to a short option
  (`-sS1`), and a negation (`--no-cached`) count. A program with `--check` that is not one of the formatters below is
  read as a write too. Write the file with the Write or Edit tool (on Codex, `apply_patch`), or write the text out in
  full in an `echo`, a `printf`, or a quoted heredoc.

These stay allowed:

- reading these files (`cat`, `grep`, `jq`, `sed -n`, and git's readers, such as `git diff` and `git log`), `git add`,
  `touch`, `mkdir .claude/agents`, and writes to other files;
- `git restore --staged` and `git rm --cached`, which change only git's index;
- a known formatter or linter run with `--check` or `--list-different` and no option that writes: `prettier`,
  `black`, `ruff`, `isort`, `taplo`, `biome`, `dprint`, `shfmt`, `rustfmt`, `eslint`, `stylelint`, and `djlint`. A long
  option that starts with or abbreviates a write option (`--write`, `--fix`, `--wr`), and a short option cluster with
  `w`, `i`, or `o` in any case (`-w`, `-O`), count as a write;
- words that are messages, not files: a `dev` command's arguments (except the file of `-o` or `--out`), `gh`'s words
  (except the file of `-O` or `--output`), and another program's word that holds a space and names a folder that does
  not exist, such as `git commit -m "docs: explain ~/.claude/settings.json"`.

Agents may not run `dev setup` (except `dev setup --preview` and `--help`) or `dev install`: the developer runs them in
a terminal, where they write these files in their own process (see
[devkit's own state, configuration, and install](#devkits-own-state-configuration-and-install)).

**Not covered yet.** The rule judges what a write adds, not what it takes away: deleting a settings file, or writing
one without devkit's hooks in it, is allowed. A path the guard cannot resolve counts as a settings file only when it
names `.codex`, `.claude`, `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, or a settings filename (`config.toml`, `hooks.json`,
`settings.json`, `settings.local.json`), or when its filename is hidden (`"$F"`) and the text turns hooks off. It
counts by the name it holds only when it is a word without a space, and never for `gh` or `dev`. When both the
filename and the text are hidden, as in `echo "$X" > "$F"`, the guard cannot tell, and the write is allowed.

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
| `dev branch prune --apply` without `--local` or `--offline` (target `origin:merged-branches`); `dev gc --apply`, `dev sync --scheduled` in every form, and `dev jobs run gc` or `sync` (target `origin:merged-branches-of-every-project`: with no `gc` job installed, the scheduled sync runs the same cleanup, which deletes merged branches on `origin` in every project) | tier 2 of the [irreversible-action gate](crew.md#guardrails), like `git push --delete`: a crew member never runs them; in the main session, while `safeguards.irreversible_main` enforces, they need your grant for the exact target, such as `grant push-destructive origin:merged-branches`. A branch glob grant never covers them. `dev release` deletes the branches it shipped under its own `release` grant. The listing, `--local`, `--offline`, `dev wt rm` and `dev branch clean` delete nothing on `origin` and need no grant |
| The third `git push` of the same branch within 60 minutes | deny unless prefixed `DEVKIT_LOCAL_CI_PASSED=1` |
| `git clean` with `-x` or `-X` | deny (it deletes `.env`, local config and caches) |
| `git reset --hard` with uncommitted changes to tracked files | deny |
| `git stash`, `git stash push`, `save`, `pop`, or `apply` (the stash is shared by every worktree) | deny → `dev commit --wip`, then `dev commit --unwip` |
| A commit, push, or tag chained after a pipe without `pipefail` (`npm test \| tail -5 && git commit …`), and the same for `dev commit`, `dev push`, `dev release`, `gh pr create`, `gh pr merge`, and `gh release create` | deny → run the check without the pipe, start with `set -o pipefail;`, or test the output |
| A wait (`dev watch --wait`, `dev ci watch --wait`) with more work after it in the same command line: after `;`, `&&`, `\|\|`, `&`, or a new line, or a pipe into anything but a read-only filter (`tail`, `head`, `grep`, `wc`, `jq`, …) | deny → run the wait alone and act after it wakes you: the wait wakes the session only when everything after it has ended |
| An action that must depend on a check's result sequenced after it without depending on it: after `;`, `\|\|`, or `&`, or after the check piped into a filter and joined with `&&` without `set -o pipefail` (`npm test; npm run release`, `npm test \| tail -3 && npm run release`). Checks are a CI watch or run, `dev test`, test runners, linters, type checkers, and `test`, `lint`, `check`, `build` scripts; actions are commit, push, tag, merge, release, publish, and deploy, a project's `release`, `publish`, or `deploy` script included (`npm run release`, `make release`, `just release`) | deny → gate it on the real result: `check && action` without a pipe, `set -o pipefail;` first, or a separate call |
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
| An agent's `gh issue create`, `gh issue comment`, `gh issue edit`, or `gh api` on the issues of devkit's own repository, a `gh api repositories/<id>/issues…` route in any repository (a numeric id does not say which one), a `gh api graphql` mutation of an issue, a comment, or a reaction in any repository (a node id does not say which one either), and an MCP tool call that writes an issue or a comment there | deny, in the main session and the crew alike, whatever grants exist: "Agents do not post to devkit's bug registry directly. Use `dev bug report` or `dev bug detour`: devkit checks what leaves this machine. A comment on a pull request goes through `gh pr comment` or `dev pr comment`." (see [The bug registry](bug-registry.md)) |
| `gh api graphql` with a query the guard cannot read: from standard input (`-F query=@-`, `--input -`), from a file it cannot open, or filled in by the shell (a substitution or a variable) | the action `unknown`, tier 2: it may be a merge or a forced ref update. Write the query out with `-f query='…'` or `-F query=@query.graphql`; if it must run as it is, the developer types `grant unknown cmd-<hash>` |

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
| `xargs rm` or `xargs unlink`, in any folder (a temp folder too: what it deletes comes from its input) | deny → `dev rm` |
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
| `dev config set safeguards.*`, `audit.*`, `bugs.share`, or `agents.*`, also spelled with leading or doubled dots (`.agents.codex`, `bugs..share`), which `dev config set` reads as the same key; `dev config edit`; or a `DEVKIT_*` or `DEV_ROOT` value added to the `env` block of an agent settings file or to a shell startup file such as `~/.zshrc` | deny: `bugs.share` decides whether devkit posts under your GitHub account, so only you set it, with `dev bug share` in a terminal. `agents.*` turns devkit on or off for an agent, and the next `dev install` or `dev setup` takes devkit's hooks and rules out of an agent turned off, so only you set it, in your terminal |
| `dev setup`, except `dev setup --preview` and `dev setup --help` | deny: it decides which agents devkit sets up and rewrites their settings, hooks, and instructions, so you run it in your own terminal. `--preview` only draws the setup's screens with sample data and changes nothing |
| Replacing, writing, or deleting a `dev` that a shell finds: `~/.local/bin/dev` (the link itself) or another `dev` earlier on `PATH` | deny → the developer runs `dev install --only cli` |
| Unsetting or blanking a variable that tells a program an agent runs it (`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_PID`, `CODEX_SANDBOX`, `CODEX_THREAD_ID`, `DEVKIT_SESSION`, `DEVKIT_AGENT`, `DEVKIT_FROM_AGENT`): `unset`, `env -u`, `env -i`, or an empty assignment before a command or after `export`, also inside `sh -c` | deny: a project's own tools read them to act differently for agents, and an agent must not hide from them |
| `HOME`, `DEVKIT_CONFIG`, `DEV_ROOT`, or another `DEVKIT_*` variable set for `dev`, `python -m devkit`, a program that imports devkit, or a nested `claude` or `codex` session; `launchctl setenv` of one of them | deny; the one-command [escape hatches](#escape-hatches) still work |
| `PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONHOME`, `PYTHONUSERBASE`, `PYTHONPLATLIBDIR`, `PYTHONEXECUTABLE`, or `PYTHONBREAKPOINT` set for the same commands | deny: they load other code into devkit's own process. `bin/dev` runs `python3 -I -S`, which ignores them, the current folder, and the user and site-packages folders (and their `.pth` files); the rule also covers `python3 bin/dev`. A repository's own tests may still set `PYTHONPATH` |
| A shell command that names a devkit copy in `~/.local/share/devkit/releases` other than the pinned release, or the releases folder itself (a `cd`, a glob, or a `find` there chooses a copy), however the path is written | deny → run `dev` from the PATH, and read a copy's files with a file tool; `dev doctor` lists the copies. An older copy may lack newer safeguards, and its repair writes its own hooks, guard, and instructions back for every session |
| `dev install`, `dev update`, or `install.sh`, also from a program's code (`subprocess.run(['dev', 'install'])`, `os.system('dev install')`, AppleScript's `do shell script`) | deny: they change what every hook and the guard run, so the developer runs them, in a terminal. A `dev install` that runs outside the developer's terminal anyway records nothing new for the extensions. A terminal an agent opens can look like the developer's, so devkit audits every acceptance (`ext.accept`) and lists it at the session start and in `dev status` until the developer runs `dev ext ack`: see [Skills and plugins](skills-and-plugins.md#who-changes-extensions-and-how-drift-shows) |
| A command `screen` starts (`screen -dm dev install`), a `tmux` shell command (`new`, `new-window`, `split-window`, `respawn-*`, `display-popup`, `tmux -c`, a name tmux abbreviates such as `new-w`), an `expect -c` script's `spawn`, `exec`, and `system`, AppleScript's `do script`, `write text`, and `keystroke`, and `xargs dev` or `xargs dev ext` (its input adds the command) | read as the command they run, for every rule: `screen -dm dev install` is refused as `dev install` is. These start the command in a terminal of their own, which passes devkit's own check for the developer's terminal |
| Typing into a `screen` or `tmux` session: `screen -X` or `-Q` (`stuff`, `exec`, and every other command sent to a session, also after `-S`, `-p`, `-r`, or `-x`), tmux's `send-keys`, `send-prefix`, `paste-buffer`, `set-buffer`, `load-buffer`, `pipe-pane`, and `command-prompt`, a tmux command that carries a command (`run-shell`, `if-shell`, `set-hook`, `source-file`, `bind-key`, `confirm-before`, a menu or chooser template), `set-option` of a hook, `default-command`, `default-shell`, `command-alias`, `lock-command`, `copy-command`, or `editor`, a `#(…)` format, `set-option` or `setw` with `-a` or `-F` and a format with the `E` or `T` modifier (`#{E:@x}`), which can build a `#(…)` from pieces, a tmux command list (`\;`, `';'`, or a command tmux knows right after a tmux call and a `;`; a new line ends the tmux call, so the next line is the shell's own command), a configuration file (`screen -c`, `tmux -f`), a tmux command it does not know, a `screen` or `tmux` word the shell or a runner fills in (`$O`, `{}`), and a `$` or a backtick anywhere in a tmux word, with spaces or without (`"$X(touch f)"` with `X='#'` is a `#(…)` format), except in the shell command that `new`, `new-window`, `split-window`, `respawn-*`, or `display-popup` starts, which the guard reads as `sh -c` | deny, also for a name tmux abbreviates such as `send-k`, inside `sh -c`, `env`, `timeout`, `xargs`, `find -exec`, or `parallel`, and in a program's code: agents cannot type into a terminal session; the developer can. Screen expands `$VAR` and `$:cap:` in what it types and `stuff -k` types a terminal key, so the rule is an allowlist that fails closed instead of a reading of the keys. `screen -ls`, `screen -r x`, `screen -dm <command>`, `tmux ls`, `tmux attach`, and `tmux new -d '<command>'` stay open |
| `open -a Terminal` or `iTerm` (also by path or `-b` bundle id, and other terminal apps), or `open` of a `.command` or `.tool` file, also from a program's code | deny: the shell it starts is one the guard cannot read, and it looks like the developer's terminal. Opening files, folders, and URLs in other apps is allowed |
| `dev update --check` (with `--quiet` and `--record` only) | allow: it only fetches tags and reports; any other word makes it the update itself, which is denied |
| `dev ext update <name> --apply`, `dev ext add`, `remove`, `enable`, `disable` (also with `--project`), `dev ext ack`, and any other `dev ext` verb | deny: they change the plugins, skills, or MCP servers that run in every session on this Mac, or confirm that what was accepted was the developer's, so the developer runs them, in a terminal. The rule is an allowlist that fails closed: `dev ext ls`, `dev ext outdated`, a team pin bump (`dev ext update <name> --to <ref> --why "…"`), and `dev ext add\|remove --team` (a change to devkit's defaults in the checkout, which goes through a PR) are allowed; `--team` counts only before a `--`, after which it is the entry's name |
| A `claude` or `codex` call whose words include `plugin`, `plugins`, `mcp`, or `marketplace` anywhere (an option's value and any case included), in the forms the guard reads: a path or link that resolves to the binary (a relative one from the folder the command runs in), Claude Code's `~/.local/share/claude/versions/<version>`, a link on the `PATH`, a package runner (`npx`, `bunx`, `bun x`, `npm exec`, `npm x`, `pnpm dlx`, `yarn dlx`, `yarn exec`, `pnpm claude`, `yarn codex`), `node …/@openai/codex/bin/codex.js`, `bash -c`, `bash -s` with its input, `xargs`, `find -exec`, `dev claude … --`, or a program's code (`subprocess.run([...])`) | deny, unless the whole command is one of the read-only forms: `claude plugin list`, `claude plugin details <name>`, `claude plugin validate <path>`, `claude plugin marketplace list`, `claude mcp list`, `claude mcp get <name>`, `codex plugin list`, `codex plugin marketplace list`, `codex mcp list`, `codex mcp get <name>`, or a group alone (`claude mcp`, which prints its help). `claude plugins` is `claude plugin`, and only `-h`, `--help`, and `--json` may come with these forms. Anything else is denied even when it only reads (`claude -- plugin list`, `claude --debug plugin list`): the rule is an allowlist that fails closed, because each block list of verbs lost to a new spelling. With `claude` or `codex`, a word the shell expands counts as one of those words: one with `$` or a backtick (`$P`, `"$@"`, `$'…'`, `$(…)`), or one without spaces with `{`, `*`, `?`, or `[` (`{plugin,install}`, `plugi?`), and a word `xargs` adds counts too. This denies `claude -p "$PROMPT"` too: put the prompt in the command. A program the shell picks at run time (`$c plugin …`, a `PATH` the command sets, a path the command has yet to create) with one of those words is denied too. See [What the guard cannot see](#what-the-guard-cannot-see) and [Skills and plugins](skills-and-plugins.md#who-changes-extensions-and-how-drift-shows) |
| `dev config lock` or `dev config unlock` | deny: they add or remove the [settings lock](agents.md#the-settings-lock) that keeps settings files from turning off devkit's hooks |
| `dev impeccable` with a verb or form off its allowlist, in any shell form. Agents run `context` (with `--target` only), `detect` on local paths, `doctor` (with `--json` or `--target`, never `--fix`), `surface-brief read`, and `critique-storage slug` and `trend`, and in write lanes and the main session also `detect` on URLs on `localhost` or `127.0.0.1`, `live`, `generate`, `document`, `extract`, and `init` (these four with no other words), and the skill's helpers in the forms it uses (the `live-…` helpers, `surface-brief write`, `critique-storage`, `comp-spec`, and `build-phase start --direction`), with `--target`, `--file`, `--artifact`, `--build`, `--decision-comp`, `--comp`, `--regions`, and the targets and snapshot file of `surface-brief` and `critique-storage` inside the project (never a URL or a route): the first word, spelled as listed, never after `skills` or a second `--`. `--id`, `--session-id`, and `--crop` take a name (letters, digits, `.`, `_`, and `-`, never `.` or `..` alone), `live-server --port` a port number, and no path, URL, or port may hold a character the shell expands (`$`, a backtick, `{`, `*`, `?`, or `[`) | deny, in the main session and both lane kinds: every other form is the developer's, in a terminal. `hooks`, `install`, `link`, `update`, `pin`, `unpin`, and `doctor --fix` write the agents' settings, hooks, or skill folders, which change what runs in every later session; `concept-seed` and `generate-image` reach the internet; `build-phase advance --force`, `build-phase start --comp` or `--reset`, and `live-complete --force` skip the skill's gates; `comp-spec --out`, `--out-dir`, and `--spec` write outside the project; the engine also has hidden verbs, and an update can add more. `dev impeccable` itself refuses them outside the developer's terminal |
| `dev impeccable` whose `--target` or `--file` leads, from the folder the shell is in after `cd`, `pushd`, or `env -C`, outside the workspace, into the handoff repository, or outside a write lane's step scope | ask in the main session, as for any write outside the workspace; deny in a write lane. `dev impeccable` itself also refuses an agent's call run outside a project checkout of the workspace, or whose path values (the ones the preceding row lists) lead out of that checkout through a symbolic link or `..` |
| `dev hook <event>`, in any shell form (a pipe, `sudo`, `bash -c`, `eval`, `find -exec`, `python3 bin/dev`) | deny, for every session: only the harness runs a hook, when its event fires. A hook event typed through a shell could forge your prompt or answer |
| A write, move, or delete of a Claude Code transcript (`~/.claude/projects/**/*.jsonl`, a subagent's too) or a Codex session file (`~/.codex/sessions`, `archived_sessions`), with a file tool, a patch, or the shell | deny: devkit reads your approvals from them. Claude Code's memory files stay writable |
| A program an agent runs (inline, from its input or a pipe, or a script outside the workspace) that loads a devkit module and reaches a function that records or uses your prompts, answers, and grants, or `devkit.state`'s writers | deny, for every session. The guard also reads the agent's own files the program loads (`sys.path` plus `import`, `python -m`, `runpy`, `exec(open(…).read())`, `SourceFileLoader`), three levels deep, and refuses code the program decodes and runs at run time. A repository's own scripts and tests are not judged by this rule |
| A program an agent runs that loads a devkit module and reaches a function behind one of [the developer's own decisions](#the-developers-own-decisions): `bugs.approve`, `bugs.drop`, `bugs.confirm`, `bugs.set_consent`, `lessons.release`, or the block of `lessonrules` | deny, for every session, the main session included: the refusal names the `dev` command to run instead and the grant line that opens it. A repository's own scripts and tests are not judged by this rule |

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

They also apply when a program that drives a terminal starts the agent: an `expect` script (by its path, `-f`, `-b`,
`--`, `-c`, or fed through its input, `expect < x.exp` or `cat x.exp | expect`), a `tclsh` program with `spawn`,
`exec`, or `system`, Python's `pexpect.spawn('codex …')`, `script -q /dev/null codex …`, and `unbuffer codex …`.
The guard reads the program where it writes the agent's flags, as it reads any interpreter's program, also one an
interpreter reads from its input (`python3 < x.py`, `cat x.py | python3`). A repository's own script is its code
and is not read this way, as for the other interpreter rules.

A nested session with your own settings still runs, and so does a prompt that only names these flags. If the work
needs another configuration, start that session yourself in a terminal.

### What the guard cannot see

The guard's rules read a command as it is typed, and the commands it hands to a shell, a package runner, `xargs`,
`find -exec`, or a program's code. A runner whose command comes from its input is refused, for every session, in the
shell and in a program's code: `xargs` with only a wrapper after it (`xargs env`, `xargs nohup`, `xargs sudo`,
`xargs timeout 5`: xargs adds what it reads, and its first word becomes the command), a placeholder in the command's
place (`xargs -I{} {} …`, `parallel env`, `find … -exec {} \;`, `dev {}`), a shell line the input fills in
(`xargs sh -c`, `xargs sh -c '"$@"' sh`), and a shell or interpreter whose script, code, or module the input names,
or that gets no program at all (`xargs sh`, `xargs python3 -c`, `xargs node -r`, `xargs -I{} node`,
`find … -exec sh {} \;`, `parallel python3 ::: a.py`). `xargs` with no command runs `echo` and stays open, and so does
a program that is named with its script (`xargs python3 tool.py`, `xargs -n1 python3 -m py_compile`). The guard reads
xargs's options as xargs does, so a value is never taken for the command (`xargs -tn 1 dev`,
`xargs --max-args 1 dev`). For GNU `parallel` it reads each string an option names for the input as the placeholder
(`-I`, `--replace`, `--er`, `--bnr`, `--dnr`, `--bner`, `--seqreplace`, `--slotreplace`, the tag of `--rpl`), and any
`{…}` in the command too (`{.}`, `{..}`, `{+/}`, `{=…=}`), so `parallel -I % % install` is refused. A command given
as one word with spaces is the shell line parallel runs (`parallel 'gzip -9 {}' ::: a.log` is `sh -c 'gzip -9 {}'`).
A command that decides what runs only when it runs in other ways gets through. For the extension rule, these are not
refused:

- a program name built at run time: `$(printf claude) plugin install x`, or an alias (`alias c=claude; c plugin …`);
- a script a shell reads from a source the guard cannot read: `curl … | sh`, `echo '…' | sh`, or a file the same
  command writes;
- a command another program starts later from text the guard does not read: a git alias that runs a shell command
  (`git -c 'alias.x=!claude plugin install y' x`), `at` or `batch` with their input, a compiled AppleScript file
  (`osascript x.scpt`), or an `expect` script file;
- options that load a plugin or MCP server for one session only: `claude --mcp-config`, `--plugin-dir`.

The [irreversible-action gate](#commands-the-guard-cannot-name) reads a program an interpreter runs, but not yet
these, and a fix is planned:

- local code the program loads: `import big_mod` of a large module beside it, `python3 -m big_mod`, `runpy`,
  `importlib`, or `exec(open(…).read())`;
- a zip app or a package folder an interpreter runs (`python3 app.pyz`, `python3 pkg/`, `node pkg/`);
- a command the program decodes when it runs (base64, rot13, hex);
- GNU `parallel` options other than its replacement strings: `--arg-sep`, `--limit`, `--filter`, `--ssh`, a profile
  (`-J`), and a Perl expression `{=…=}` in an argument of a named program (`parallel echo '{=system(…)=}'`).

The guard's rules do not read quoting or brace expansion around a command word yet; a fix is planned. `$'dev' ext ack`,
`$"dev" install`, `dev {ext,} ack`, and `$'screen' -X …` reach the shell as `dev` and `screen`, but the guard does not
take them for those commands. devkit's own terminal check (`set_by_developer()`) still refuses those `dev` commands
when an agent runs them directly. One line form is not read as bash runs it yet either, and a fix is planned too:
lines between two comments that each hold an apostrophe (`ls # it's` and `dev install # don't` on the next line),
because the pair reads as one quoted word. A command split by a backslash-newline is read as typed, with every
backslash-newline removed (`dev \` with `install` on the next line is `dev install`), and with the backslash at the end
of a comment dropped (bash ends a comment at its newline); a refusal in any of the three readings stands.

Typing into a terminal session is refused outright: agents cannot type into a `screen` or `tmux` session at all. The
guard refuses each command that types or carries a command, by its name, an alias, or a prefix tmux accepts; a `#(…)`
format and what can build one from pieces (`set-option -a` or `-F`, a format with the `E` or `T` modifier); a tmux
command list; a configuration file; a word the shell or a runner fills in; and a `$` or a backtick in any tmux word
but the shell command a tmux command starts. It refuses them at the top level, in `sh -c`, `env`, `timeout`,
`xargs`, `find -exec`, and `parallel`, and in the string literals of a program that starts a process; a literal the
guard cannot read that names `tmux` or `screen` is refused as unreadable. A command the guard does not read at all (a
script file, a command built at run time) is outside this rule, as
[What the guard cannot see](#what-the-guard-cannot-see) says.

The rules on git and on deleting files read each word as typed, so they miss what the shell expands
(`git push {--force,origin} main`), a script a shell reads from its input (`bash <<< 'git push --force …'`), and a
command `pnpm` or `yarn` runs by its name (`pnpm git push --force …`).

For the extensions, drift and the acceptance list are the backstop: whatever changes outside devkit shows at the
session start, in `dev status`, and in `dev doctor`; the developer accepts it only with `dev install` in a terminal;
and every acceptance stays listed until the developer runs `dev ext ack`, because a terminal an agent opens can look
like the developer's. See
[Skills and plugins](skills-and-plugins.md#who-changes-extensions-and-how-drift-shows).

### Commands the guard cannot name

The [irreversible-action gate](crew.md#guardrails) classifies each command by the program it runs and the command it
gives it. When the shell names the program only when it runs, the guard cannot know what it does, so the command is the
action `unknown`, tier 2, with the target `cmd-<hash>` of that exact command text. For example:

- `$(printf gh) pr merge 12`, `${X:-gh} pr merge 12`, `read g; $g pr merge 12`, or `env -S 'gh pr merge 12'`;
- `gh $SUB merge 12` or `gh pr $ACT 12`, where a variable names gh's command, and `git $SUB push`, where a variable
  names git's;
- a program that builds the name of what it calls, such as `getattr(__import__('os'), 'sys' + 'tem')`, or starts a
  command that begins with what it fills in at run time, such as ``execSync(`${tool} pr merge 12`)`` or
  `subprocess.run(f"{tool} …")`;
- a program or script the gate must read and cannot: larger than it reads (512 KB for a program an interpreter runs
  or a script a shell runs, 256 KB for a shell script run by its path) or unreadable, such as `python3 big.py` padded
  past 512 KB with a force push at its end. A program run by its path is read as the system runs it: with a python,
  node, or other interpreter's shebang as that interpreter's script (`./big.py` is `python3 ./big.py`, with the same
  cap), and an executable text file with no shebang, or with a byte-order mark before `#!`, as the `sh` script the
  shell runs it as (also one that is not executable yet when the command runs `chmod`). A shell or interpreter reads its program from a redirect or a pipe the same way: `sh < f`, `< f sh`, `sh 0< f`,
  `sh <(cat f)`, `python3 < f`, `python3 /dev/stdin < f`, and `cat f | python3` are read like `sh f` and
  `python3 f`, and `source` and `.` read their input or a descriptor the same way: `. /dev/stdin <<< '…'`,
  `echo '…' | source /dev/stdin`, `source /dev/fd/3 3<<< '…'`, and `source <(echo …)` run the text they are fed
  (`source /dev/fd/3` with nothing the command shows on that descriptor is `unknown`). The file is resolved as the
  system opens it before the guard compares it: after `--`, in the folder the command runs in, with `//`, `.`, `..`,
  and symbolic links read, so `source -- /dev/stdin`, `/dev/fd//0`, `cd /dev && source stdin`, and a link to
  `/dev/stdin` are the input too; the same holds for a shell's or an interpreter's script (`bash //dev/stdin`). When
  the guard cannot tell which file `source` or `.` reads (a variable, a substitution, an option it does not read)
  and the command feeds it a here-string, a heredoc, a redirect, or a pipe, the command is `unknown`:
  `f=/dev/stdin; source $f <<< '…'`. A path the system cannot encode (a lone surrogate) counts as unreadable. A binary is classified by its
  name, as before. A shell script the gate reads but cannot parse (a comment with an apostrophe, such as
  `# it's a test`) is unknown in the same way, however it runs: `./y.sh`, `sh y.sh`, `bash y.sh`, `. y.sh`,
  `source y.sh`, `sh < y.sh`, or `cat y.sh | bash`. A script inside the workspace is the repository's own and is not
  counted as unknown when it is large (autotools' `./configure`) or cannot be parsed, as for `python3 tools/x.py`;
- a subcommand that `xargs`, `parallel`, or `find -exec` fills in for a program whose subcommand the gate reads (npm,
  pnpm, yarn, cargo, docker, twine, terraform, gem, make, and the other publish and deploy tools): `echo publish |
  xargs npm`, `xargs -I{} npm {}`, `parallel npm ::: publish`. The guard counts a filled word up to the second word
  that is no option, so `xargs npm install` and `find … -exec docker load -i {} \;` count too. `git push` with any
  word the input fills in (`echo --force origin main | xargs git push`) is `unknown` as well.

These are not `unknown`:

- a known program in a folder a variable names, such as `$VENV/bin/python` or `"$VIRTUAL_ENV/bin/pytest"`, unless
  the same command sets that variable or `IFS`;
- a variable the command itself sets from a literal word: `py=python3; $py -m unittest …`, or a `for` loop over
  literal words, `for P in python3 /usr/bin/python3; do $P -m unittest …; done`. The guard judges the command once
  for each value, as if it were written out, and one refusal refuses it: `G=gh; $G pr merge 12` is a merge. The value
  counts only from a plain assignment that always runs first (at the top of the command, after `;` or a newline,
  not in a block, subshell, pipeline, or background job, and not as a prefix such as `py=x $py`), or inside the body
  of the loop, and only when nothing else in the command can give the variable another value. These keep it
  unknown: its name anywhere else in the command, in any word as the shell reads it once quotes are removed
  (`read "G"`, `declare 'G'=…`, `read p""y`, `printf -vG`), or a use other than a plain `$G` or `${G}`; anything
  that can set, unset, or evaluate a variable by a name it computes when it runs (`read "$n"`, `printf -v`,
  `declare`, `typeset`, `local`, `readonly`, `mapfile`, `getopts`, `vared`, `trap`, `eval`, `source`, `.`, `${!n}`,
  zsh's `${(P)n}`, arithmetic, `setopt`, a function or an alias), also when a resolved variable holds such a word
  (`R=read; $R "$n"`), while `read`, `unset`, `wait`, and `export` count only with a computed word or an option
  other than the plain ones (`read -r line` is fine); a name the shell sets by itself (`REPLY`, `_`, `MATCH`,
  `BASH_REMATCH`, `OPTARG`, `PWD`, …); a value with a `$`, quote, glob, or `~`, or one that starts with `=`; `IFS`,
  `case`, heredocs, backticks, ANSI-C quotes (`$'…'`), and more than 16 combinations;
- a shell that only parses its program: `bash -n x.sh`, `sh -n`, `zsh -n` or `--noexec`, `bash -o noexec`, with a
  script, a `-c` line, or its input. It runs nothing, neither its startup files nor `BASH_ENV`, so it is a read of the
  file. `+n` or `+o noexec` after it, `-i`, `-l`, or `--login` (where `-n` does not hold), or `-n` after the script
  (a word of the script) make it run, and it is judged as running, as `csh` and `tcsh` are. For `bash` and `sh`,
  every option word must be one the guard knows runs nothing (`-norc`, `-noprofile`, `-posix`, set's letters and
  `-o` names, and `-O extglob` or another globbing option); `-login`, `-rcfile`, `-debugger`, `-O extdebug`, `-D`, an
  unknown option, or `-posix` after a short option (which bash reads as `-p -o … -s -i -x`) make it run;
- text inside an interpreter's string literals: `${…}`, `{…}`, `[…]` and `*` there belong to the language (a
  template literal, an f-string, a list), and the guard looks for spawn calls in the program's code, not in the
  text it carries. A program named in such a string (`os.system('gh pr merge 12')`) is still classified;
- backticks and `$(…)` in the strings of a Python program that starts processes only with argument lists, such as a
  regular expression `` r"`([^`]+)`" `` or a Markdown template. Such a program uses no `shell=` (other than `shell=False`),
  `os.system`, `popen`, `getoutput`, or `pty`, names no shell or `-c` option in a string, and builds no code with
  `exec`, `eval`, `compile`, `__import__`, or `getattr`. With any of those, its strings are read as shell lines,
  as before.

A Python variable named `HOME`, `DEV_ROOT`, or `DEVKIT_*` (`HOME = Path.home()`) is a variable of the program, so it
does not count as setting devkit's environment. `os.environ["HOME"] = …`, `putenv`, an `env={"HOME": …}` dictionary,
and `dict(os.environ, HOME=…)` still do.

A command line a program hands to a shell or a process (`subprocess.run('…', shell=True)`, `os.system`, `os.popen`,
Node's `child_process.exec` and `execSync`, Ruby's `system` and backticks, Perl's `system`) is judged by the rules of
the same line typed in the shell, with the variables the program sets in its own environment counted as set:
`os.system('HOME=/tmp/x dev status')` is refused as `HOME=/tmp/x dev status` is, and so is `os.environ["HOME"] = …`
followed by `os.system('cd /tmp && dev status')`.

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
publishes a project's branches at all. An embargo holds an agent's pushes of a branch and its commits, so pushing an
embargoed branch is the developer's decision too, and so are posting to the [bug registry](bug-registry.md) under
your GitHub account (your consent, and each held item), confirming a detour that every agent then gets, and releasing a
held [lesson](lessons.md#held-lessons) that every agent then recalls. These commands are the action `developer-only`,
tier 2:

| Command | Grant line that opens it once |
| --- | --- |
| `dev branch embargo --lift <branch>` | `grant embargo <branch>` |
| `git push`, `dev push`, `dev pr create` or `gh pr create` that sends an embargoed branch, or commits its embargo holds (under another name, a commit id, a branch made from it, a tag, `--all`) | `grant embargo <branch>` |
| `git push`, `dev push`, `dev pr create` or `gh pr create` of any branch while devkit cannot read the project's handoff note | `grant embargo <branch>` |
| `git push`, `dev push`, `dev pr create` or `gh pr create` from a folder known only when it runs (`cd "$X"`, `git -C "$(…)"`, `GIT_DIR=$G`, anything after `source` or `.`) or from a folder that is no project (a temporary clone, a folder that does not exist yet), while any project on this Mac has an embargo or devkit cannot read an embargo list | none: the target is `?`; the agent writes out the path of the project's checkout instead |
| `dev backup --public` | `grant backup <project>` |
| `dev backup policy on\|off\|ask` | `grant backup <project>` |
| `dev bug send <id>` · `dev bug drop <id>` | `grant bug-send <id>` · `grant bug-drop <id>` |
| `dev bug confirm <bug> <detour>` | `grant bug-confirm <bug>/<detour>` |
| `dev bug share on\|reports\|off` | `grant bug-share <setting>` |
| `dev lesson release <lesson>` | `grant lesson-release <lesson>` |
| `dev lesson prevent <lesson> --by guard --match "…"\|--error "…" --block` | `grant lesson-block <lesson>`, because a lesson rule that refuses skips the warnings that show whether it is right; the block holds on this machine only (see [Lessons › Lesson rules](lessons.md#lesson-rules)) |

A program that imports devkit and calls the functions behind these commands is refused too, with the command and
its grant line (see [devkit's own state, configuration, and install](#devkits-own-state-configuration-and-install)).

A push from inside a repository's git folder (`<repo>/.git`, `<repo>/.git/worktrees/<name>`), or one that
`env -C <dir>` or `sudo -D <dir>` runs in another folder, is judged as the project of that folder, so
`grant embargo <branch>` in that project opens it. The handoff repository's own pushes are never held.

The embargo is not yet off every remote. A push is judged by the embargo list of the project in the folder it runs
from, so the embargo holds pushes from the embargoed project, its git folders and worktrees, temporary clones, and
folders that are no project. A copy of its commits in another project's folder (another clone or checkout that
devkit knows as a different project) is judged by that project's list, which does not name them, so an agent's push
from there is not held yet. devkit 26.11.4 closes this.

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
`backup-policy/<project>`, `lesson-block/<lesson>`) works too. None of them is ever pre-approved with `dev crew mode auto --approve`. A dry run,
the help, `dev branch embargo` (the list), and `dev backup policy` (the read) need no grant.

### Lesson rules

A team can add its own rules, made from lessons that keep repeating (see
[Lessons › Lesson rules](lessons.md#lesson-rules)). The guard checks them after every other rule on this page, on
shell commands only, and they can only make its verdict stricter:

| Rule mode | Decision |
| --- | --- |
| `warn` | allow, with the lesson's title and prevention added to the call's context |
| `block` | deny: "devkit · lesson rule LR-… refuses this call, from lesson L-…: … Do it the way the prevention says." |

A lesson rule never allows a call that the guard's own rules refuse or ask about. A rule file the guard cannot read, a
lookup that takes longer than 0.3 seconds, or any other failure lets the call go on as the guard's own rules decided,
with a `guard.failopen` record.

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
| A crew member records or withdraws a decision (`dev crew decide`, `dev crew withdraw`) | refused by `dev crew`: only Morpheus records the developer's answers and withdraws a decision |
| A crew member writes or deletes in the session scratchpad outside its own folder, `scratchpad/<member id>/` (a file tool, a redirect, `rm`, `mv`, `find -delete`, a nested shell) | deny, with the folder to use. The member's brief names that folder, and devkit creates it when the lane opens (see [The crew › Scratch folders](crew.md#scratch-folders)) |
| A crew member runs `dev progress …` or `dev crew ask\|answer\|…` | routed to its own lane when it is the whole command, or the one lane call in a list of `cd` and reads (see below); otherwise refused, since it would act as Morpheus |

A message is data, so quoted operators in it (`dev crew ask "x; y && z"`) keep the call a single command. A `$(…)` or
backtick that the shell expands in a message runs a command of its own, so it makes the call more than one command.

#### Lane calls in a list

A member may join its lane call to `cd` and reads with `&&`, `||`, `;` or a newline, and pipe it into a read-only
filter. These reach the member's lane:

```bash
cd ~/Development/.worktrees/acme/app/fix-login && dev progress step "login fixed" --check "12 tests pass"
dev crew inbox 2>&1 | tail -5
git status --short && dev crew inbox
dev progress note "tests green" --next "commit" 2>&1 | tail -3
```

devkit puts `DEVKIT_FROM_AGENT` in front of the lane call and nowhere else. The command is routed only when all of
these hold:

- It holds exactly one lane call. Two lane calls are two commands: run them one after the other.
- The lane call starts its pipeline, and only filters that read follow it: `head`, `tail`, `grep`, `rg`, `wc`, `cat`,
  `sort`, `uniq`, `cut`, `tr`, `nl`, `column`, `jq`, and `sed` that only prints.
- Every other command reads: `cd`, or a command the read-only lanes may run (below). An interpreter, a shell, a
  project check, or anything that writes makes the list unroutable.
- The shell reads the list as written: no `$(…)` or backticks (except a quoted heredoc message), no subshell, `{ }`
  group, `if` or loop, comment, background job, or heredoc, and redirects only to `/dev/null` or between stderr and
  stdout.

Anything else stays refused: `dev progress step x && rm -rf build`, `echo y | dev crew decide D1 A`,
`dev progress step x | tee out`, `env dev crew …`, `bash -c 'dev crew …'`, and a lane call inside `$(…)`, `<(…)`,
or `>(…)` (`wc -l <(dev crew decide D1 A)`).

#### What read-only lanes may run

A review or read-only member runs only what its allowlist names: readers, reads of git, gh and dev, and the project's
checks. These run as well:

- An interpreter or shell asked only for its version: `python3 --version`, `bash --version`, `python3 -V`.
- `command -v` and `command -V`, which only look names up and run none of them: `command -v gh dev`.
- A shell that only parses its program: `bash -n x.sh`, `zsh -n -c '…'` (see
  [Commands the guard cannot name](#commands-the-guard-cannot-name)).
- Machine diagnostics: `ps`, `pgrep`, `sw_vers`, `id`, `whoami`, `printenv`, `uptime`, `groups`, `locale`,
  `getconf`, `seq`, and `sysctl` without a value, `-w`, `-f`, or `-p`.
- A command substitution or process substitution whose own command is on the allowlist:
  `git diff $(git merge-base HEAD main) HEAD`, `diff <(git show a:f) <(git show b:f)`. Arithmetic `$((…))` and a
  substitution used as the program stay refused.
- `sed` whose script only prints, with or without `-n`: `sed 's/a/b/g' f`, `sed '/^#/d' f`. `-i`, `-f`, and the `w`,
  `r`, and `e` commands stay refused.
- `awk` whose program prints to standard output only. A program with `>`, `|`, `system()`, `getline`, `@load`, or a
  program file stays refused; compare with `<` instead of `>`.
- `mkdir -p` inside the member's own scratch folder, `scratchpad/<member id>/`.
- A shell variable set on its own, when its name is lowercase or one capital letter (`S=/path; ls $S`). `PATH`,
  `PAGER`, `IFS`, and zsh's tied names (`path`, `fpath`, …) stay refused.
- `dev guard check`, and `dev lesson show` and `dev lesson ls`.
- The reads of work state: `dev overview`, `dev standup`, `dev team`, `dev initiative`, `dev initiative ls`,
  `dev initiative show`, `dev report`, `dev plan`, and `dev plan ls`. They change no record and
  refresh only devkit's own caches of pull requests and agent providers, as `dev status` does. `dev report -o` (or
  `--out`), which writes a file, `dev initiative new`, `add`, and `rm`, and `dev plan approve` and `supersede` stay
  refused.
- `dev plan new` and `dev plan revise`, for a member whose kinds of work produce plans: one the team's RACI makes
  Responsible for requirements, architecture, research, UX, or docs, in a review lane. They write only below
  `plans/<the run's project>/` of the handoff repository. `dev plan new` runs from that project's checkout, without
  `-p` or `-i`; `dev plan revise` takes the full path of one Markdown file whose real path lies in that folder, with
  links and `..` resolved. Either one runs only as a command of its own: with a `cd`, `pushd`, `popd`, or subshell
  earlier in the same command (`cd ../other && dev plan new x`), the guard cannot be sure which folder it runs in, and
  refuses it. When devkit cannot look up the member's kinds of work or the project, it refuses.
- A command continued with a backslash-newline, read as the shell joins it.

A script from the member's own scratch folder (`python3 scratchpad/<member id>/probe.py`) already ran before this
change, and still does; a script in another member's folder does not.

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

- A command the tokenizer cannot read (a quote that is never closed, such as an apostrophe in a comment) is refused,
  also in a line the command hands to a shell (`sh -c`, `eval`, a heredoc a shell reads), because bash may still run
  the lines before it. In a program that starts a process, a string literal the guard cannot read is refused when it
  names `dev`, `devkit` (as in `python3 -m devkit`), `claude`, `codex`, `tmux`, `screen`, `osascript`, or `open` as
  a whole word
  (`os.system("dev install # don't")`); any other literal, such as `print("don't")`, is data. Heredoc bodies are set
  aside first, so text with apostrophes can go in one. A script file or a sourced file the guard cannot read is
  allowed, as before.
- When the guard itself fails while it judges a call that acts (a shell command, `Monitor`'s included, a `Write`,
  `Edit`, `MultiEdit`, `NotebookEdit`, or `apply_patch`, a `WebFetch`, whose rule keeps secrets out of the URLs it
  sends, or an MCP tool), the call is refused with the reason "devkit's guard could not judge this call." A crash would
  otherwise skip every rule for that call, and an agent shapes the command text, so it could choose one that crashes
  the guard. The refusal is logged to `~/.local/state/devkit/logs/hooks.log` with its traceback and
  recorded as `guard.deny` with rule `guard-error` (`dev audit --action guard.deny`). A refusal the guard had already
  reached also stands when a later part of the hook fails, recorded once, under its own rule. The agent rewrites the command more simply or stops and
  tells the developer, who can run it in their own terminal (hooks run only in agent sessions) or start the session
  with `DEVKIT_GUARD=off`, which the hook reads before any rule runs.
- Other exceptions in the guard are logged there and the call is allowed, with a `guard.failopen` record: a call that
  only reads or launches (`Read`, `Grep`, `Glob`, `WebSearch`, and `Agent`, `Task`, and Codex's `spawn_agent`, whose
  subagent's calls each pass the guard again), and a call every rule has already allowed when only the context the hook
  adds to it fails, a crew member's `dev progress` or `dev crew` call that devkit routes to its lane included. A
  guard bug never blinds the session that has to diagnose it, and the other hooks (session start, prompt, stop) still
  fail open. These checks fail closed too: a crew member's shell command that the lane check cannot read or judge;
  an irreversible action, in enforce, when the gate itself fails and the text looks irreversible; the MCP gate; and
  the developer-only actions (lifting an embargo, `dev backup --public`, `dev backup policy`).
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
