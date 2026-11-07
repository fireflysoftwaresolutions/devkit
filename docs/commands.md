# Commands

This page lists every `dev` command and option. `dev help` (or `dev -h`) prints the grouped list, and
`dev <command> -h` prints the options of one command.

A `<query>` is a project id, a project name, a path, or part of an id. When more than one project matches and you are
in a terminal, an fzf picker opens.

Commands that fail print a reason on stderr and exit with a non-zero code: `2` for usage problems (an unknown or
ambiguous project, a bad argument, a missing `--why`), `1` for an operation that did not complete, and `130` when you
cancel a picker or press Ctrl-C.

## Home

### `dev`

Prints the devkit wordmark with the tagline ("Every repo in its place. Every agent on the same page."), the version,
and a greeting. Inside a project it shows the project, its branch and state, and the last status block in two lines
(`last`: what was done, by which agent, and when; `next`: the next step). Then the workspace in one line (projects,
worktrees, live sessions, the last sync), how often agents followed the rules this week, and up to five items that
need attention, similar ones grouped, each with the command that fixes it.

```text
  Good evening.

  billing  acme/billing ────────────────────────────────────────────────────────
  feat/PROJ-142-invoice-export  clean
  last  added the CSV export and its tests (claude, 12 min ago)
  next  open the PR once the local suite is green

  Workspace ────────────────────────────────────────────────────────────────────
  14 projects · 6 worktrees · 2 live sessions · synced 3 min ago
  agents followed the rules in 97% of replies this week, 91% in the full format

  dev status · dev ls · dev dashboard · dev help
```

In a narrow terminal the wordmark becomes one line.

## Projects

### `dev ls [--all] [--offline] [--names] [--json]`

The projects on this Mac: those with a checkout or a worktree here. One row per project, newest activity first: the
alias you type, the branch, your changes in words ("78 to push, 1 new file"), the checks of the branch (`✓`, `✗`, `…`,
or `–` when there are none), the number of worktrees, and the last activity. At 100 columns and wider the row also
shows the size and, when the alias differs, the repository name. `●` marks a project a session is working in now, and
`!` one outside devkit's folder layout (`dev adopt` fixes it). A footer counts the portfolio projects that are not on
this Mac.

| Option | Effect |
| --- | --- |
| `--all` | Also list the portfolio projects that are not on this Mac, grouped by owner, with the `dev clone` command |
| `--offline` | Do not query GitHub; use cached PR data and skip the checks |
| `--names` | Print only the project ids, one per line (with `--all`, every project) |
| `--json` | The rows as JSON, with the portfolio projects that are not on this Mac |

### `dev go <name>`

Prints a project's path. `<name>` is an id, an alias, or part of a name (see `dev alias` for how names match). When
the match is not exact, a dim line on standard error names the project it picked (`kit → kit-consulting`). The `dcd`
shell function uses it.

```bash
cd "$(dev go widget)"      # what `dcd widget` does
```

### `dev alias [<alias> [<project>] [--move] | rm <alias>]`

Short names for projects, kept in the handoff repository (`aliases.json`) so every Mac and teammate shares them. Every
project has an alias: the repository name when no other project has it, or the one you set. `dev status`, `dev go`,
`dev open`, `dev wt new --repo`, `dev claude`, `dev codex` and `dev clone` accept an alias, and completion offers them.

| Command | Effect |
| --- | --- |
| `dev alias` | The aliases of the projects on this Mac and every alias the team set: `ALIAS`, `PROJECT`, `SET BY` |
| `dev alias <alias> [<project>]` | Give the project (default: the one you are in) this alias; it replaces the one it had |
| `dev alias <alias> <project> --move` | Take an alias that another project has |
| `dev alias rm <alias>` | Remove an alias you set; the project gets its repository name back |

An alias uses lowercase letters, digits, `.`, `-` and `_`, starts with a letter or a digit, and has at most 40
characters. It cannot be another project's alias (unless you pass `--move`) or another repository's name.

A name matches projects in this order, ignoring case: an exact id, alias or path; an exact repository name; a name or
alias that starts with it; one that contains it. The first level with a match decides. Within it, one match wins, or
the only match on this Mac. Otherwise devkit lists the matches, each with its command, and exits with `2`: it never
guesses.

### `dev status [<name>] [--all] [--json]`

The project you are in, or `<name>`, in the order of its questions:

1. **Needs you:** what only you can do, or `✓ Nothing needs you right now.` A crew decision waiting for you, the last
   update's You bullet, a project outside devkit's folder layout (with its `dev adopt` command), and work at risk.
2. **Right now:** the checks of the branch or its open PR, the crew members working now (role, name, and what their
   step does, never its code), and the plan's steps done and time left while a run works. A run works while it or one
   of its crew reported in the last 30 minutes.
3. **Last update:** the latest status block, whole: who and when, its title, Done, Check, and Next. It names the
   worktree or copy it came from when that is not the one shown.
4. **Branches:** where each branch lives, one row each (the first eight; `dev branch ls` has them all): local only
   or local plus its remote, ahead and behind its upstream, how far it is from the default branch, its backup, its
   embargo, its pull request, and its linked task. A branch with commits that exist only on this Mac, older than
   `backup.after_minutes` (30), reads `not backed up` with the reason.
5. **Details:** the branch in words, the last commit, worktrees, the folder, the handoff note and the project's
   commands.

```text
Branches
  release/26.11.3        local only      ↑209 · 209 ahead of main · not backed up: 209 commits only
                                         on this Mac, oldest commit 7 h ago
  feat/dashboard-server  local only      merged into release/26.11.3 · nothing to back up
  main                   local + origin  ↑0 ↓0 · default branch · nothing to back up
```

Text wraps at the terminal width, or at 100 columns in a wider one, under its column; nothing is cut. Outside a
project it shows what needs you across this Mac's projects, then the projects (the `dev ls` table).

| Option | Effect |
| --- | --- |
| `--all` | Also the project's tasks (Now, Next, Later) and plans |
| `--json` | The same data as JSON, for tools |

### `dev clone <owner/repo | host/group/repo | url | name>`

Clones into the canonical path and prints it. GitHub shorthands go through `gh repo clone`; everything else goes
through `git clone`. The new repository is marked trusted in Codex and gets a handoff note. A bare name or alias of a
portfolio project (`dev ls --all` lists them) clones that project.

```bash
dev clone flyquery                             # a portfolio project, by its name
dev clone acme/widget                          # → ~/Development/acme/widget
dev clone git@gitlab.com:group/sub/svc.git     # → ~/Development/gitlab.com/group/sub/svc
dev clone gitlab.acme.io/team/api              # → ~/Development/gitlab.acme.io/team/api (https)
```

If the path already exists, it prints the path, warns, and exits with `0`.

### `dev new <name> [--github [--owner OWNER] [--public]] [--no-git]`

Without `--github`, creates `~/Development/_local/<name>` with `git init -b main` (or a plain folder with `--no-git`).
With `--github`, creates `OWNER/<name>` on GitHub (private unless `--public`, with a README) and clones it into the
canonical path. `--owner` defaults to you.

### `dev adopt <path>`

Moves a repository that is not at its canonical path:

1. Refuses (exit `3`) while any process works in the repository or its worktrees. Close those sessions and shells
   first.
2. Moves worktrees placed outside the repository to `~/Development/.worktrees/<project id>/<name>`
   (`git worktree move`).
3. Moves the main checkout (with any nested `.claude/worktrees`) to the canonical path and runs
   `git worktree repair`.
4. Moves the matching Claude Code history folders in `~/.claude/projects`, so `claude --resume` keeps working.
5. Trusts the new path in Codex, logs the move, and syncs.

Restart Claude Desktop and Codex app conversations that were opened at the old path from the new path.

### `dev open [<query>]`

Opens the project in the configured `editor` (default `code`).

## Portfolio

These commands read and write the handoff repository. [Teams and machines](teams.md),
[Initiatives](initiatives.md), and [Progress and communication](progress-and-communication.md) explain the model
behind them.

### `dev dashboard [--no-open | --status | --stop | --export FILE | --path]`

Starts the dashboard server in the background, or reuses the one that runs already, and opens the live command center
in your browser with a one-time sign-in link. The server listens on `127.0.0.1` only, reads only, and stops by itself
after `dashboard.idle_minutes` (30) with no open page. A server from an older devkit version is replaced. See
[Dashboard](dashboard.md).

| Option | Effect |
| --- | --- |
| `--no-open` | Starts or reuses the server and prints the one-time link (valid once, within 60 seconds) instead of opening the browser |
| `--status` | Whether a server runs: its address, pid, devkit version, snapshot number, open pages, and start time. Exits with `1` when none runs |
| `--stop` | Stops the running server (only a devkit dashboard process) |
| `--export FILE` | Writes a static, self-contained copy of the page to `FILE`, without a server |
| `--path` | Writes the static copy devkit keeps (`~/.local/state/devkit/dashboard/index.html`) and prints its path |

### `dev overview [--json] [--days N] [--all]`

The portfolio digest for you and your agents: every initiative (status, priority, progress bar, milestone, due date,
member projects, task counts, Now tasks), then every project (status, priority, live sessions, purpose, the latest
status, its branches when some are not backed up or embargoed, Now tasks, open task count, the last session line),
then the items that need attention. The heading counts the branches not backed up across the portfolio; `--json`
gives each project's `branches` as `{total, not_backed_up, embargoed, waiting}`.

| Option | Effect |
| --- | --- |
| `--json` | Machine-readable output for agents and scripts |
| `--days N` | Window for the commit count of each project (default 7) |
| `--all` | Also list archived projects and initiative members that are not cloned on this Mac |

### `dev standup [--days N]`

What happened in the last `N` days (default 1), per project: tasks completed, your commits, session lines, and the
current Now tasks.

### `dev initiative [new | add | rm | ls | show]`

Cross-project initiatives. See [Initiatives](initiatives.md).

| Command | Effect |
| --- | --- |
| `dev initiative` · `dev initiative ls` | Table of initiatives: status, priority, progress, milestone, due date, number of projects, open tasks |
| `dev initiative new <slug> [--title "…"] [--projects a/b,c/d]` | Creates `initiatives/<slug>/README.md` from the initiative template, with one workstream per member project |
| `dev initiative add <slug> <project>` | Adds a member project (and its workstream heading) |
| `dev initiative rm <slug> <project>` | Removes a member project |
| `dev initiative show <slug>` | The initiative: progress and where it comes from, milestone, due date, goal, every member with status and progress, and the Now tasks |

The guard refuses `new`, `add`, and `rm` from inside a project session: membership is a portfolio decision, so run them
from `~/Development` or ask the developer.

### `dev task [-p PROJECT | -i SLUG] [--all] [<verb> …]`

Work items of the project you are in, of another project with `-p`, or of an initiative with `-i`. Without a verb
(or with `ls`) it lists the open items in board order (Now, then Next, then Later; high priority first; oldest first).
The number in front of each item is what the other verbs accept. `--all` lists every done item instead of the last
five.

`-p` and `-i` work before or after the verb. The examples put them after it:

```bash
dev task done 3 -p acme/billing
dev task add "Agree on the event schema" --now -i payments
```

A `<selector>` is an id (`T-…`, `F-…`, `B-…`, `S-…`, or a unique prefix), the number from the list, or a unique part
of the title. See [Teams and machines](teams.md) for locks, versions, and the changelog.

| Verb | Effect |
| --- | --- |
| `add <title> [options]` | Creates a work item (see the next table) |
| `start <selector> [--why]` | Claims the lock (8 hours by default), pushes it at once, and moves a todo item to Now as doing. Run in the project's checkout on a work branch, it links the branch (`branch:<name>` in the item's links, in the private handoff), so the branch map shows the task without its id in the branch name |
| `claim <selector> [--why]` | Same as `start` |
| `release <selector> [--why]` | Gives the lock back; the item returns to todo |
| `done <selector> [--why]` | Marks the item done and releases the lock |
| `block <selector> --why "…"` | Marks the item blocked |
| `unblock <selector> [--why]` | Back to work: todo, or doing while the item is still claimed |
| `cancel <selector> --why "…"` | Cancels the item and clears its lock |
| `reopen <selector> --why "…"` | Puts a done or cancelled item back to todo, for an item closed too early; refused on an open item |
| `move <selector> --to now\|next\|later --why "…"` | Moves the item between buckets |
| `edit <selector> [--title "…"] [--body-file FILE] [--priority high\|normal\|low] [--due YYYY-MM-DD] --why "…"` | Changes the title, the description, the priority, or the due date |
| `delegate <selector> --to LOGIN [--why "…"] [--due YYYY-MM-DD] [--no-copy]` | Assigns the item to a teammate, releases your lock, prints the message to send, and copies it to the clipboard |
| `link <selector> --to SLUG --why "…"` | Counts a project task toward an initiative (adds the link `initiative:<slug>`) |
| `show <selector>` | The item's fields, its description, and its history |
| `history <selector>` | The item's changelog only |
| `ls` | The open items (the default) |

Options of `dev task add`:

| Option | Effect |
| --- | --- |
| `--now` · `--next` · `--later` | The bucket (default Next) |
| `--type task\|feature\|bug\|spike` | The kind of work; the id starts with `T-`, `F-`, `B-`, or `S-` and the description uses that type's template |
| `--priority high\|normal\|low` | Priority (default normal) |
| `--due YYYY-MM-DD` | Due date |
| `--why "…"` | Why the work exists; recorded in the changelog and filled into the template |
| `--done "…"` | An acceptance criterion; repeat it for more than one. Fills the template's `Done when` checklist |
| `--claim` | Claims the new item at once |
| `--body "…"` | Your own description instead of the template |
| `--no-template` | No description at all |

`--why` records the reason in the item's changelog and in its commit. `edit`, `move`, `block`, `cancel`, `reopen`,
and `link` refuse to run without it. Changing an item that someone else holds an unexpired lock on
is refused; coordinate with them or wait for the lock to expire.

### `dev note set <key=value> … [-p PROJECT | -i SLUG]`

Sets metadata in the frontmatter of a project note (or of an initiative note with `-i`).

| Key | Values |
| --- | --- |
| `status` | `active` · `paused` · `blocked` · `done` · `archived` |
| `priority` | `high` · `normal` · `low` |
| `milestone` | Free text |
| `due` | `YYYY-MM-DD` |
| `tags` | Comma-separated, `tags=payments,api` |
| `progress` | `0`–`100`, a manual value that overrides the progress computed from tasks; empty to compute it again |

Initiatives also accept `title`, `owner`, `projects` (comma-separated) and `source`.

```bash
dev note set status=blocked priority=high
dev note set milestone="Refunds live" due=2026-12-15 -i payments
```

### `dev plan [new | revise | approve | supersede | ls] [-p PROJECT | -i SLUG]`

Versioned plans and specs in `plans/<scope>/`. See [Templates](templates.md#plans-and-specs) and
[Teams and machines › Versioned plans](teams.md#versioned-plans).

| Command | Effect |
| --- | --- |
| `dev plan new "<title>" [--kind plan\|spec\|spike\|bugfix\|adr]` | Creates `plans/<scope>/<date>-<slug>.md` from the template of that kind (default `plan`), at version 1, status draft |
| `dev plan revise <file> --why "…"` | Records a change: bumps the version, logs who, when, why, and the size of the change, and commits. A plan written without devkit (for example by Superpowers) is adopted into the format on its first revision |
| `dev plan approve <file> [--why "…"]` | Sets the status to approved and records a version |
| `dev plan supersede <file> --why "…"` | Sets the status to superseded and records a version |
| `dev plan` · `dev plan ls` | Every plan (or the plans of `-p` / `-i`): id, status, version, title, path, and `✎ needs dev plan revise` when the body changed without a revision |

### `dev team [--hours N]`

Who is working on what: one entry per developer and machine (`user@host`) that published presence in the last `N`
hours (default 24), with its live sessions (agent, project, branch, latest status) and the locks it holds. Each
machine publishes its own presence file when it syncs.

### `dev progress [start | step | add | assign | begin | reopen | skip | block | done | history | time]`

Live progress of long agent work, with active time, a critical-path ETA and a plan that stays true. See
[Progress and communication](progress-and-communication.md).

| Command | Effect |
| --- | --- |
| `dev progress start "<goal>" [--plan "a=30m; b=1h after a [backend]; c"] [--estimate 2h] [--steps N] [--mode guided\|auto] [--replace --why "…"]` | Starts a run for the current session. While the session's run is live, a second start is refused: finish it with `dev progress done`, or replace it with `--replace --why "…"`, which keeps the reason in the new run. Each plan step can carry an estimate, the steps it waits for, and its kind of work in brackets: an id from `dev crew raci`, which decides the RACI check and the approver ([The crew › Kinds of work](crew.md#kinds-of-work-and-the-raci)); an unknown id is refused. Steps without an estimate share what `--estimate` leaves. `--mode` sets who takes the developer's decisions ([The crew](crew.md#guided-or-automatic)). Prints how long similar runs took here |
| `dev progress step "<what finished>" --check "<evidence>" [--step <n or name>] [--next "…"] [--force]` | Closes the running step (or the one named) with its evidence, and prints the update, which is the status block with the plan under it, then the exact change made. Refused when the description does not match the step, a crew member still owns it, or its dependencies are open; `--no-check` when there is nothing to verify |
| `dev progress step "<what finished>" --check "<evidence>" --approves "api, screens"` | The step's Check is also its verdict on the finished steps it names: `✓` approves them, `✗` or `⚠` sends them back. Refused, whole, when a named step is not done, has no kind of work, was done by the same member, or is approved by someone else in the RACI |
| `dev progress note "<what is happening>" [--next "…"]` | An update while a step is still running: recorded and printed as the status block (Check `–` the step is still running), and no step changes state. From a crew member, it stays in its lane |
| `dev progress add "<name>=<estimate> [after …] [<id>]" [--why "…"]` | Adds a step found during the work; add it when it starts. `[<id>]` declares its kind of work, as in a plan. From a crew member, the step goes to its own lane |
| `dev progress assign <step> --to <member> [--scope "<globs>"] [--activity ID] [--allow push,pr] [--why "…"]` | Hands a step to a crew member; its time is that member's, and the guard keeps its edits inside the scope. `--activity` declares the step's kind of work (an id from `dev crew raci`, such as `backend` or `ux`), so the RACI check and the approver do not depend on the language of the step's name. `--allow` gives the member push and PR create in that step, each only with the developer's grant (`grant push <branch>`) or a verified decision. Refused when the crew's RACI does not make that member Responsible for the step's kind of work, unless `--why` gives the reason, which is recorded |
| `dev progress begin <step>` | Starts a step in parallel with the running one |
| `dev progress reopen <step> --why "…"` · `dev progress skip <step> --why "…"` | Undoes a wrong close, or skips a step on purpose |
| `dev progress block "<why>" [--next "<what is needed>"]` | The run is blocked; prints the update and sends a desktop notification |
| `dev progress done "<outcome>" [--why "…"]` | The run is finished; refused while steps are pending, decisions wait, or an Accountable member has not approved the work they own. `--why` ends it without that approval and records the reason. Records the active time against the estimate and sends a desktop notification |
| `dev progress history` | Every step change: when, from → to, by whom (the tech lead (Morpheus), a crew member, devkit), why |
| `dev progress` | Every run of the last 12 hours on this Mac, and the runs of other machines from their presence |
| `dev progress time [--days N] [-p PROJECT]` | Same as `dev time` |

Estimates accept `45m`, `1h30m`, `2h`, or a number of minutes. Inside a subagent, `dev progress start`, `step`, and
`add` write to that subagent's own lane (the hooks pass its id), never to the main plan; `assign`, `begin`, `reopen`,
`skip`, `done`, and `start --replace` change the main plan and are refused there. Run them as a command of their
own: inside `&&`, a pipe, `$(…)`, or a redirect, the guard refuses them. devkit records every shell call with the
agent that made it, and a command is the tech lead's (Morpheus's) only when such a call holds it as typed. The tech lead types these
commands too: through a script or another shell, they are refused (see [The crew › Who is
calling](crew.md#who-is-calling)).

### `dev time [--days N] [-p PROJECT]`

How long things take, over the last `N` days (default 7): turn length (median, p90, longest), how accurate past
estimates were and the factor that corrects new ones, subagent durations, the slowest commands by total time, and the
runs in progress.

### `dev report [--days N] [-i SLUG] [-p PROJECT] [-o FILE]`

A status report in Markdown for the team or for management, built only from the handoff repository: a summary table,
then per initiative its health with the reason, progress, milestone, due date, owner, goal, what was done in the
period and by whom, what is in progress, what is next, blockers, and risks, and who worked on it; then the projects
outside initiatives. `--days` sets the period (default 7), `-i` or `-p` limits it to one initiative or project, and
`-o` writes it to a file. The health rules are in
[Progress and communication › Status reports](progress-and-communication.md#status-reports).

### `dev handoff-import <staging> --slug SLUG [--dry-run]`

Adopts an older handoff or program repository as an initiative, from a prepared staging folder. `--dry-run` prints
what it would import. The staging format is in [Initiatives › Adopt an existing handoff
repository](initiatives.md#adopt-an-existing-handoff-repository).

## The crew

devkit's specialist agents. See [The crew](crew.md).

### `dev crew [graph | raci | rules | show | brief | route | delegate | decline | next | ask | answer | inbox | mode | decide | decisions | set | history | sync]`

| Command | Effect |
| --- | --- |
| `dev crew [--json] [--latest]` | The roster, and in a session with a run: who is working now, the steps assigned to each member, open messages and decisions, and the mode. `--latest` reads the most recent running run on this Mac instead of this session's; `--json` prints the roster and the live state |
| `dev crew graph [--latest] [--mermaid \| --json]` | The live session as one graph, with the tech lead (Morpheus), the run's steps with their state, owner, kind of work, and the steps each waits for; the crew lanes and nested subagents under their steps; the work items the run tracks; decisions waiting for the developer; approvals owed; and open messages. `--latest` draws the most recent running run on this Mac; `--mermaid` prints a Mermaid flowchart that renders on GitHub; `--json` prints the nodes and edges |
| `dev crew raci [<work> \| <member> \| "<task>"] [--markdown]` | Who does (R), approves (A), is consulted on (C), and is informed about (I) each kind of work, as one table. With a kind of work, its row; with a member, what they do, approve, and are consulted or informed on; with a task, the kind of work it reads as, and its row. `--markdown` prints the whole matrix as a Markdown table |
| `dev crew raci <work> --set "R=tank A=seraph C=… I=…" --why "…"` | Changes one row of the RACI for the whole team, recorded in the handoff repository with who and why |
| `dev crew rules` | Every crew rule, the devkit check that enforces it, and its state (enforced, planned, or guidance). Exits with `1` when a rule's check is missing |
| `dev crew set <member> <field> "<value>" --why "…"` | Changes a member's definition for the whole team (`name`, `title`, `role`, `summary`, `goal`, `skills`, `procedure`, `tools`, `tier`, and the other fields), recorded in the handoff repository. List fields take `"a; b; c"`; `procedure` takes JSON. The members get it in their briefs; Codex roles with `dev install --only codex` |
| `dev crew history` | The team's changes to the crew and the RACI, newest first: what changed, who changed it, and why |
| `dev crew show <name>` | One member: role, personality, when to use it, skills, tools, model tier, keywords |
| `dev crew brief <name> "<task>" [--scope "<globs>"] [--step <step>] [--activity ID]` | A ready brief for the Agent tool or `spawn_agent`, the exact launch line for Claude Code and Codex, and the `dev progress assign` command that links the lane. `--activity` declares the task's kind of work, which picks the brief's skills, knowledge, and known pitfalls in any language; without it, the member's Responsible kind of work decides |
| `dev crew route "<task>" [--files a,b]` · `dev crew route --plan [--apply]` | The best members for a task, with scores and reasons; or a member for every unowned plan step |
| `dev crew delegate "<task>" [--to <name>\|auto] [--scope …] [--after <step>] [--estimate 30m] [--files a,b]` | A step for another member, with who asked; joins a working member's lane, or waits for the tech lead (Morpheus) to launch it |
| `dev crew decline "<step>" --why "…"` | Hands a delegated step back to the tech lead (Morpheus) |
| `dev crew next` | Ready to launch (with the commands), queued until a slot frees, waiting and why, and who is busy |
| `dev crew next --first <step> --why "…"` | The tech lead (Morpheus) launches that step ahead of the order; the reason is recorded and audited. Both options are required |
| `dev crew ask "<text>" [--to <name>]` · `dev crew answer <id> "<text>"` · `dev crew inbox` | Messages between members; delivered with the recipient's next action |
| `dev crew ask --decision "<question>" --options "A: <choice> \| B: <choice>" --recommend A [--irreversible push] [--target v26.11.4] [--performs A] [--approves "api, screens"]` | A decision point: a question under 300 characters, 2 to 4 options each with its letter, and the recommended one (refused otherwise, with the corrected command; an irreversible action without options is asked as `A: yes \| B: no`). Decided at once in auto mode, waits for the developer in guided mode or for irreversible actions. `--target` is what the irreversible action acts on, and the question names it as a word of its own; the developer's answer opens that action on that target once, and only when it chooses the option that performs it: `--performs`, else the one option that reads yes or approve, else the recommended one ([The crew › Guided or automatic](crew.md#guided-or-automatic)). `--approves` names the finished steps the developer's answer approves, checked against the plan |
| `dev crew mode [guided\|auto] [--approve "push feat/*, release v26.11.4"]` · `dev crew decide <id> "<answer>"` · `dev crew decisions [--pending] [--ask [<id>…] [--json]]` | The mode (`--approve` names each action with its target; publish and deploy never), the developer's answers (recorded by the tech lead (Morpheus) only), and every decision with who took it. `--ask` prints the pending decisions (or the ones named) as question tool calls, at most 4 per call, the recommended option first, `--json` for the tool's input; the tech lead may reword a reversible one. Only an answer to a question with `D<n> ·` up front (or the header `D<n>`) that picks one of its options by letter or words, or a typed `approve <id>[: <option>]`, makes the decision the developer's. An irreversible one counts only as printed: its question, its labels word for word (the performing option's label says what it runs, `A: yes (release v26.11.4)`), and no descriptions |
| `dev crew sync [--check]` | Regenerates the Claude Code agents from `config/crew.json` |

## Lessons

Learn from mistakes: blameless lessons in the handoff repository, recalled in every session, counted when they repeat,
and turned into automation. The guide is [Lessons](lessons.md).

### `dev lesson [add | anchor | ls | show | recur | prevent | retire | restore | scan | review] [-p PROJECT | -i SLUG | --global]`

The scope is the project you are in (or `-p`), an initiative (`-i`), or `--global`. `<lesson>` is an id, a unique
start of one, or words of a title.

| Command | Effect |
| --- | --- |
| `dev lesson add "<title>" --cause "…" --prevent "…" [options] [anchors]` | Records a lesson; the root cause and the prevention are required. Options: `--kind bug\|mistake\|process\|tooling\|security\|performance\|design`, `--severity low\|medium\|high`, `--happened`, `--fix`, `--detect`, `--evidence` (repeatable), `--tag` (repeatable), `--retire-when`, `--why`. Anchors say where it applies, each repeatable: `--file "<glob>"`, `--command "<command words>"`, `--activity <id>` (a kind of work from `dev crew raci`), `--error "<text the tool output shows>"`, `--stack <stack>`. Without anchors, only the session start can recall it, and the command says so |
| `dev lesson anchor <lesson> --why "…" [--file …] [--command …] [--activity …] [--error …] [--stack …]` | Adds anchors to a lesson, for example one written before anchors existed; they join the ones it has. An anchor cannot contain a comma, and an unknown kind of work is refused |
| `dev lesson [--search WORDS] [--all] [ls]` | The lessons of this scope plus the global ones, strongest first. `--search` keeps those with the words in the title or any section, and `--all` lists every scope and retired lessons. Both options belong to `dev lesson` itself: put them before `ls`, or leave `ls` out, because after `ls` they are refused |
| `dev lesson show <lesson>` | All fields, sections, and the history |
| `dev lesson recur <lesson> [--evidence "…"] [--why "…"]` | It happened again: counts the repeat, adds the evidence, and reopens a retired lesson |
| `dev lesson prevent <lesson> --by test\|lint\|type\|guard\|skill\|ci\|rule\|doc --ref "…" --why "…"` | Automation now prevents it; the lesson leaves the session context |
| `dev lesson retire <lesson> --why "…"` | It no longer applies |
| `dev lesson restore <lesson> --why "…"` | Brings an archived lesson back as open |
| `dev lesson scan [--days N]` | Signals worth a lesson that have none yet: reverts, fixes, repeated guard refusals, missed status blocks, bouncing tasks (default 14 days) |
| `dev lesson review` | Recurring lessons that are not prevented, and open lessons not seen for four months |

### `dev sleep [--dry-run] [--quiet]`

Consolidates and forgets, like a brain at night: merges near-duplicate lessons, archives lessons that no longer help
into `lessons/archive/`, keeps at most 25 active lessons per scope, proposes global lessons for patterns seen in
two or more projects, and trims devkit's own old records, caches, and logs. The `sleep` job runs it every night;
`--dry-run` shows what would change. See [Lessons › Sleep](lessons.md#sleep).

## Agents

### `dev claude [<query>] [-w BRANCH] [-- ARGS…]` · `dev codex [<query>] [-w BRANCH] [-- ARGS…]`

Starts Claude Code or Codex in a project (default: the current one). With `-w BRANCH` the agent runs in the managed
worktree for that branch, which is created when needed. Arguments after `--` go to the agent.

```bash
dev claude widget -w feat/search      # Claude Code in ~/Development/.worktrees/<id>/feat-search
dev claude widget -- --resume         # pass arguments to claude
dev codex -- resume --last            # resume the last Codex session in the current project
```

### `dev impeccable <verb> [<args>…]`

Runs the engine of the impeccable design skill, the one `dev install --only skills` put in devkit's skills folder, in
the current folder. Each word after `dev impeccable` reaches the engine as one argument, as typed, with no shell in
between. Before every run, the command checks the engine's SHA-256 against the install record
(`~/.local/state/devkit/skill-engines.json`) and refuses an engine reached through a symbolic link out of devkit's
skills folder. A refusal runs nothing and names the fix, `dev install --only skills`. Otherwise the command exits
with the engine's exit code.

The verbs that write the agents' settings, hooks, or skill folders (`hooks`, `install`, `link`, `update`, `pin`,
`unpin`, and `doctor --fix`) are the developer's: the command runs them only in your terminal, and the guard refuses
them to every agent. Crew members run impeccable this way, never by the launcher's path, which the guard refuses.
Read-only lanes run only `context`, `detect` on local paths, and `doctor` without `--fix`. See
[Skills and plugins › impeccable and its engine](skills-and-plugins.md#impeccable-and-its-engine).

```bash
dev impeccable context                  # load PRODUCT.md, DESIGN.md, and the surface brief; print what to follow
dev impeccable detect --json src        # scan the project's HTML and CSS
dev impeccable doctor                   # the engine's own checks
```

### `dev handoff [<query>] [--edit | --path | --log "…" [--agent AGENT]]`

Prints the project's handoff note after refreshing its generated blocks.

| Option | Effect |
| --- | --- |
| `--path` | Prints the note's path |
| `--edit` | Opens the note in `$EDITOR` (then the configured editor, then `vi`) |
| `--log "…"` | Appends a line to the project's session log (`sessions/<project id>/YYYY-MM.md`) with the date, you, your machine and the agent, and commits it |
| `--agent` | Who logs the line: `human` (default), `claude` or `codex` |

### `dev session brief | carry | compactions | forget`

A session's continuity: what devkit keeps for a session so that its run and its open requests survive compaction,
`/clear`, and resume.

The hooks give the same brief after a compaction or a resume; these commands read it where no hook runs. devkit keeps
the developer's last 5 requests, redacted and cut to 1,000 characters, only for turns classed as development work, and
deletes a record 7 days after its last change. The brief is at most 4 KB, and `carry` is refused while a member of
that run is still running.

| Verb | Effect |
| --- | --- |
| `brief` | Prints what devkit kept for a session: this session by default, another with `--session <agent>:<session id>`, or the project's newest session with an open run or requests in the last 12 hours with `--latest` (`-p` names the project). `--for-chat` cuts each request to 200 characters for a chat surface; `--provider` names the reader, and the request text is shown only to the session's own provider |
| `carry` | Moves the run into this session after `/clear`; `--from` names the cleared session when it is not the one `/clear` ended |
| `compactions <transcript>` | Counts a transcript's compactions with their tokens before and after; `--members` adds the members' transcripts. Reads only |
| `forget` | Deletes a session's continuity records (`--session`), or every record and `/clear` link here (`--all`); recorded in the audit trail |

## Worktrees

### `dev wt new <branch> [--base REF] [--repo QUERY] [--no-deps]`

Creates (or reuses) the managed worktree `~/Development/.worktrees/<project id>/<branch>` and prints its path. The
branch name must follow the naming rule (see [Guard rules › Branch names](guard.md#branch-names)). Progress messages
go to stderr.

| Option | Effect |
| --- | --- |
| `--base REF` | Start point for a new branch (default `origin/<default branch>`) |
| `--repo QUERY` | The project, when you are not inside it |
| `--no-deps` | Do not clone `node_modules` from the main checkout |

### `dev wt ls [--all]`

The worktrees of the current project (or of every project with `--all`): branch, PR, state, location (`canonical`,
`claude`, `codex`, `temp`, `stray`, `missing`), live session or idle time, size, and path.

### `dev wt rm <branch | path>`

Removes a worktree only when it has no uncommitted, untracked, or unpushed work and no process uses it. The local
branch is deleted when all its commits are on a remote. A registered worktree whose folder is gone is pruned. Exits
with `1` when removal was refused.

## Delivery

### `dev ci [<query>] [--commands] [--runs]`

Reads `.github/workflows/*.yml` and prints each workflow's triggers, jobs, runners, matrices, setup actions, and every
`run:` command, so you can run exactly what CI runs before the single push. Expressions (`${{ … }}`) are shown as
written. The query can also be `status`, `watch`, `audit`, or `init`; each has its own entry below.

| Option | Effect |
| --- | --- |
| `--commands` | Only the `run:` commands, in order (steps that use secrets are left out) |
| `--runs` | Also list the 8 most recent CI runs (`gh run list`) |

### `dev ci --run | --mark-passed | --evidence`

Local CI evidence: proof that what CI runs passed on your Mac, on exactly the files you push. `dev push`,
`dev pr create`, and `dev release` require it. See [Git and GitHub › Local CI evidence](git-and-github.md#local-ci-evidence).

| Option | Effect |
| --- | --- |
| `--run` | Runs the `run:` steps of the workflows that run on pushes and pull requests, in order, and records the evidence when all pass. Setup actions, dependency installs, side effects, and steps that need CI secrets are skipped, with the reason |
| `--dry-run` | With `--run`: the plan, without running anything |
| `--keep-going` | With `--run`: do not stop at the first failure |
| `--step-timeout MIN` | With `--run`: minutes per step (default 30) |
| `--resume` | With `--run`, after a fix: re-runs the failed steps, the steps that never ran, and the passed steps that the changes since can affect; a test step re-runs only its failed and affected tests (the whole step when the change is global). The evidence is recorded as "resumed" (see [Testing › After a CI step fails](testing.md#after-a-ci-step-fails)) |
| `--mark-passed [--note "…"]` | Records that you ran the full suite yourself on the current files; the note goes into the PR body |
| `--evidence [--json]` | Whether HEAD has evidence, and how it was recorded; exits with `2` when it has none |

### `dev ci status [--watch] [--branch NAME | --all-branches] [--all-jobs] [--limit N] [--json]`

The live state of the CI/CD runs of the repository you are in, on any of four hosts; the provider follows the
`origin` remote. `dev ci <project> --status` does the same for another project.

```text
  GitHub Actions · acme/billing · feat/invoice-export ──────────────────────────
  ▸ ci #142 running · 12m40s elapsed · ETA ~6m20s (usually 19m00s) · push a1b2c3d
      3/5 jobs done · 2 running
      ▸ tests (Python 3.13) · step 4/7 “Unit tests” · 6m10s
      https://github.com/acme/billing/actions/runs/…
```

Each run shows its state, the elapsed time, and an ETA: the median duration of the last successful runs of the same
pipeline, minus the elapsed time. Failed jobs come first, then the running ones with their current step. When the
newest run on GitHub failed, the command prints `gh run view <id> --log-failed` and exits with `1`.

| Option | Effect |
| --- | --- |
| `--watch` | Refresh every 15 seconds until no run is in progress |
| `--branch NAME` | Runs of this branch (default: the current branch) |
| `--all-branches` | Runs of every branch |
| `--all-jobs` | List every running job instead of the first six |
| `--limit N` | Number of runs (default 5) |
| `--json` | The normalized runs as JSON |

| Host | How devkit reads it | Verified |
| --- | --- | --- |
| GitHub Actions | `gh`, signed in by devkit | Live |
| GitLab CI | `glab api` when installed (`glab auth login`), otherwise the REST API with `GITLAB_TOKEN` | Against recorded API responses |
| Bitbucket Pipelines | The REST API with `BITBUCKET_TOKEN` (an access token with `pipeline:read`), or `BITBUCKET_USERNAME` and `BITBUCKET_APP_PASSWORD` | Against recorded API responses |
| Azure Pipelines | The REST API with `AZURE_DEVOPS_EXT_PAT` (a token with the Build read scope), the same variable the `az devops` CLI uses | Against recorded API responses |

Tokens are read from the environment and never printed. A host that cannot be read reports why instead of failing.
`dev status` shows the latest run of the branch, and when you ask an agent for status, the prompt hook adds the
branch's pipelines to its context.

### `dev ci watch [-p PROJECT] [--branch B] [--sha S] [--timeout 90] [--json] [--quiet]`

Follows the CI/CD runs of a branch for one commit (default: the current branch at HEAD) until they finish, on GitHub
Actions, GitLab CI, Bitbucket Pipelines, or Azure Pipelines. A failure comes with the failed jobs and steps, the first
error of the failed log (GitHub through `gh run view --log-failed`, GitLab through `glab ci trace`; Bitbucket and
Azure give the job, step and link), its class (`test`, `lint`, `build`, `dependencies`, `pipeline`,
`infrastructure`, `unknown`) and the one next action. The log excerpt is marked as untrusted output and has secrets
redacted. Exit codes: 0 passed, 1 failed, 3 still running at the timeout, 4 no run appeared within five minutes.

devkit starts it in the background after every `git push`, `dev push`, `gh pr create`, `dev pr create`,
`gh pr merge`, `dev pr merge`, and `dev release` that a main session runs (one watcher per repository, branch, and
commit), and the result reaches that session with its next action: a crew message to the main agent when it has a
progress run, otherwise a note on its next tool call or prompt. Each result is in `~/.local/state/devkit/ci/watch/`,
the audit trail (`ci.watch`), and a desktop notification. `DEVKIT_NO_CI_WATCH=1` turns the automatic start off. See
[CI/CD pipelines › Watch every run](ci-cd.md#watch-every-run).

### `dev ci audit [--fix] [--strict] [--provider HOST] [--json] [-p PROJECT]`

Diagnoses every pipeline in the repository (GitHub Actions, GitLab CI, Bitbucket Pipelines, Azure Pipelines) for
speed, reliability, security, quality gates, and delivery. Each finding has an id, a severity, the file and line, and
the exact fix. On GitHub it adds `actionlint` and `zizmor` findings, on GitLab `glab ci lint`, when those tools are
installed. The findings catalog is in [CI/CD pipelines](ci-cd.md#findings-catalog).

| Option | Effect |
| --- | --- |
| `--fix` | Applies the safe GitHub Actions fixes in the working tree (least-privilege `permissions`, `concurrency`, job timeouts, actions pinned by SHA, the draft gate) and never commits; for the other hosts, prints the snippet under each finding |
| `--strict` | Exit `1` on medium findings too (default: high and critical) |
| `--provider github\|gitlab\|bitbucket\|azure` | Only one host |
| `--json` | The report as JSON |

Exit codes: `1` when a high or critical finding remains (with `--strict`, a medium one too), `0` otherwise.

Draft pull requests: `full-ci-on-draft` (low) flags a pull request workflow that runs every job on a draft, so each
push to a draft runs the whole CI. `draft-gate-no-ready` (medium) flags a workflow whose jobs skip drafts but whose
`types` leave out `ready_for_review`, so marking a draft ready starts no run until the next push. `--fix` writes the
gate: `types: [opened, synchronize, reopened, ready_for_review]` on `pull_request`, and
`if: github.event.pull_request.draft == false` on every job that needs no other job (the jobs that need it skip with
it; on a push the field is null, which equals false, so pushes still run).

### `dev ci init [--provider HOST] [--stack STACK] [--dry-run] [--force] [-p PROJECT]`

Writes a pipeline for the detected stack and host: the host from the `origin` remote (GitHub when there is none), the
stack from the lockfiles (`--stack auto|python|node|java-maven|java-gradle|go|php|docker`). On GitHub it writes
`.github/workflows/ci.yml`, `.github/workflows/release.yml` and, without Dependabot or Renovate, `.github/dependabot.yml`;
on the other hosts, their single pipeline file. Every action is pinned by SHA and every image by digest; the pipeline
has lint, tests with a JUnit report, a security job, and on release tags an SBOM, signing or attestation, and a check
of what was published. On GitHub, `ci.yml` skips draft pull requests and runs once when one is marked ready for
review (the draft gate under `dev ci audit`). It audits the result, and on GitHub runs `actionlint` on it.

| Option | Effect |
| --- | --- |
| `--dry-run` | Prints the files and their audit; writes nothing |
| `--force` | Replaces an existing pipeline file (refused without it) |
| `--provider github\|gitlab\|bitbucket\|azure` | Another host than the remote's |
| `--stack STACK` | Another stack than the detected one |

Exit codes: `1` when the file exists without `--force`, or the result has a medium or higher finding, or `actionlint`
rejects it; `0` otherwise. See [CI/CD pipelines › Set up a pipeline](ci-cd.md#set-up-a-pipeline).

### `dev calver [--next]`

The repository's current CalVer tag, the next version (`YY.MM.PATCH`), and the names of its tag and release branch. `--next` prints only the next version. Outside a repository it reads the devkit repository.

### `dev delivery [-p PROJECT | -i SLUG] [--json]` · `dev delivery apply …`

A project's delivery policy: how it versions, tests, releases, and ships. See
[Delivery › The delivery policy](delivery.md#the-delivery-policy).

`dev delivery` shows three things: what it detects in the repository (kind, version scheme and latest tag, version
file, release tools, changelog, CI, the test gate, branch model, what it publishes and deploys), the recorded policy
or the one it recommends, and the gaps, each with its fix. `--json` prints the same for tools and agents. With `-i`,
it prints the initiative's recorded policy (a release train).

`dev delivery apply --why "…" [--set KEY=VALUE]… [--tasks] [-p PROJECT | -i SLUG]` records the policy (the recorded
one, or else the recommendation) in the handoff repository with the changes from `--set`, and makes one commit.

| Option | Effect |
| --- | --- |
| `--why "…"` | Required: the reason, recorded in the policy's history and the commit |
| `--set KEY=VALUE` | Change one field; repeat it. Lists (`test_gate`, `publish`, `deploy`) are separated by `;` |
| `--tasks` | Open a work item for every gap (high gaps go to Now, with high priority) |

The fields are `versioning`, `tag_format`, `version_source`, `release_flow`, `changelog`, `test_gate`, `ci`,
`branch_model`, `publish`, `deploy`, `kind`, `release_train`, and `bump`.

```bash
dev delivery apply --why "first release policy: service, CalVer, Maven verify gate" --tasks
dev delivery apply --set versioning=semver --set test_gate="./mvnw -B verify" --why "it is a library now"
dev delivery apply -i platform --set release_train="bom → parent → services, monthly" --why "release together"
```

### `dev sec scan [<query>] [--history] [--config RULES] [-v]`

Scans the current repository (or `<query>`) for security problems. Each scanner is optional; a missing one is reported
with its install command. Exits with `1` when any scanner reports findings.

| Scanner | Finds |
| --- | --- |
| gitleaks | Secrets in the working tree, and in the git history with `--history` |
| osv-scanner | Known vulnerabilities in lockfiles and manifests |
| Semgrep | Code patterns (rule set `p/default`, or `--config`); metrics are off |
| Trivy | High and critical vulnerabilities, misconfiguration (Dockerfile, Terraform, Kubernetes) and secrets; telemetry and version checks are off |
| zizmor | GitHub Actions workflows: template injection, untrusted triggers, unpinned actions, broad permissions (runs offline, medium severity and up) |

`-v` lists up to 60 findings per scanner instead of 8.

### `dev docs check [<paths>…] [--online] [--fix] [--no-prose]`

Checks documentation in one pass, covering Markdown structure (markdownlint), links and anchors (lychee), prose (Vale with the
Google and write-good styles) and, when no paths are given, every SVG in the repository. The repository's own
configuration files win over devkit's defaults.

| Option | Effect |
| --- | --- |
| `<paths>` | Files or folders to check (default: the whole repository) |
| `--online` | Also check external links |
| `--fix` | Apply markdownlint's automatic fixes first |
| `--no-prose` | Skip Vale |

### `dev diagram render | check | new`

Branded, validated SVG diagrams from JSON specs. See [Diagrams](diagrams.md).

| Command | Effect |
| --- | --- |
| `dev diagram new <kind> <name> [--dir DIR]` | Scaffolds a spec: `flow`, `c4`, `state`, `sequence`, `er`, or `timeline` |
| `dev diagram render <spec \| dir> [--out DIR] [--png]` | Writes `<name>-dark.svg` and `<name>-light.svg` (specs in `docs/diagrams/` go to `docs/images/diagrams/`); `--png` adds 2x previews |
| `dev diagram check <spec \| svg \| dir>` | Validates specs or SVG files; exits with `1` on problems |

## Git and GitHub

Branches, commits, pushes, pull requests, CI runs, releases, and issues, with the delivery rules built in. Each
command prints one line or a short table, and a refusal exits with `2` with the command that fixes it. The guide is
[Git and GitHub](git-and-github.md). `dev pr`, `dev run view`, `dev run rerun`, `dev issue`, and the release page
of `dev release` need GitHub (or a host listed in `gitx.github_hosts`).

### `dev branch new | rename | ls | clean | embargo`

| Command | Effect |
| --- | --- |
| `dev branch new <type>/<topic> [--ticket KEY] [--base REF]` | Fetches, then creates the branch from `origin/<default>` (or `--base`) and switches to it; uncommitted changes come along. The name is normalized: `feat/Invoice Export --ticket PROJ-142` → `feat/PROJ-142-invoice-export` |
| `dev branch rename <type>/<topic> [--ticket KEY]` | Renames the current branch before its first push (or names a detached HEAD); refuses permanent branches and branches already on `origin` |
| `dev branch ls [--offline] [--json]` | The branch map: every local branch, where it lives (local only, or local and its remote), ahead and behind its upstream and `origin/<default>`, the branch that has its commits (`merged into …`), its last backup or why it is not backed up, its embargo, worktree, PR (draft or ready, checks) and linked task. `--offline` uses the cached PR list; `--json` prints the rows (below) |
| `dev branch clean [--apply] [--offline] [--json]` | The local branches that are merged, whose PR was merged, or that are gone upstream with every commit on the remote; deleted only with `--apply` |
| `dev branch embargo <branch> --why "…" [--until "<step or date>"]` | Keeps the branch off the remote (see [Guard rules › The developer's own decisions](guard.md#the-developers-own-decisions) for what an agent's push is held from until 26.11.4): `dev backup` and `dev sync` never push it, nor a branch that holds any of its commits. Stored in the `embargo:` list of the handoff note's metadata (with who and when), so every machine sees it; audited as `git.embargo`. The record also keeps the branch's tips (`commits:`, updated by every backup run), so the embargo holds its commits after the branch is merged into another and deleted. Every branch view shows it. Without a branch, lists the embargoes (`--json` too) |
| `dev branch embargo --lift <branch>` | Ends the embargo; the next backup may push the branch and the branches that carry its commits. The developer's own decision: devkit refuses it from a crew member's lane, and the guard asks the main session for the developer's grant (`grant embargo <branch>`) |

The same rows feed the handoff note's generated block (one line per branch, with dates instead of "4 min ago"),
`dev status`, `dev overview`, the session-start context and the dashboard, and `registry.json` keeps them as
`projects[].branches`. Each row of `dev branch ls --json`:

| Field | Meaning |
| --- | --- |
| `name`, `sha`, `current`, `default`, `default_branch` | The branch, its tip, whether it is checked out here, and whether it is the default branch |
| `upstream`, `upstream_remote`, `upstream_gone`, `ahead`, `behind` | Its upstream and how far it is from it (`0` without one) |
| `base`, `base_ahead`, `base_behind` | How far it is from `origin/<default>` (or the local default branch) |
| `where` | `local + origin` or `local only` |
| `last_commit`, `unpushed`, `oldest_unpushed` | Unix times; `unpushed` counts the commits that are on no remote branch |
| `merged_into` | The branch that has every commit of this one, or `null` |
| `last_backup` | The newest `git.backup` audit record for the branch, or `null` |
| `embargo` | `{why, until, who, when}` from the `embargo:` list of the handoff note's metadata, or `null` |
| `embargo_via` | The embargoed branch that carries this branch's commits (it is merged into it), or `null` |
| `worktree`, `worktree_missing`, `changes`, `session` | The linked worktree that has it checked out, its uncommitted files, a live session |
| `pr` | `{number, state, draft, checks, url, title}`, or `null` |
| `task` | The work item that links `branch:<name>`, or whose id is part of the name: `{id, title, status}`, or `null` |
| `backup` | `{state, reason}`; `state` is `backed_up`, `on_remote`, `merged`, `waiting`, `not_backed_up`, `embargoed` or `no_remote` |

### `dev backup [<branch>…] [--all] [--accept-ci] [--public] [--dry-run] [--json]`

Copies the commits that exist only on this Mac to the remote, under the same branch name, when that push starts no
CI run. Without a branch it takes the current one; `--all` takes every local branch with commits on no remote. The
push is fast-forward only and never forced, and it happens only when every check holds, in this order. The checks
look at exactly the commits the push sends: those that the push target lacks (the branch's remote; `git ls-remote`
lists what it has), also when another remote has them.

1. Not the default branch, nor a long-lived one (`main`, `master`, `develop`, `trunk`, `gh-pages`), and a name that
   follows the branch naming rule. Not a branch a GitHub Actions workflow of the commit deploys from (its push
   `branches:` names it, and a job has an `environment:` or a deploy or publish step), not even with `--accept-ci`
   or `--public`: a push there is a release step, made with `dev push`.
2. No embargo on the branch, nor on any of the same commits: those of an embargoed branch, or of its recorded tips
   after it was merged into another branch and deleted (a merged branch defers to the branch that carries its
   commits), unless the push target has them already. When devkit cannot read the handoff note (it is missing with its repository, unreadable, or its
   metadata does not parse), no branch is backed up: an unreadable note never reads as no embargo.
3. The remote branch is not ahead or diverged. A diverged branch stops with the commands to resolve it
   (`git fetch`, `git log --left-right`, `git rebase origin/<branch>`, or `dev push --force-with-lease` on your own
   branch).
4. No WIP commit, and the project's backup policy allows it (below). Then, on a public repository, no commit that
   reads as a security fix (a `security` scope, a CVE, a red-team test): devkit suggests `dev branch embargo` first,
   and `--public` pushes it anyway. Public or private is the visibility of the repository the push goes to, not
   origin's.
5. The CI configuration of the commit it sends shows that the push starts no CI run. It reads the GitHub Actions
   `push` branches, `branches-ignore`, tags and paths filters, the pull request of the branch (a draft whose jobs
   skip drafts starts none, looked up on the repository the push goes to), and the jobs' `if:`; GitLab rules, Bitbucket `default` and `branches`, Azure `trigger` and `pr`. CI that
   devkit cannot read (Jenkins, CircleCI, Netlify and others) means it cannot tell, and it does not push.
   `--accept-ci` pushes anyway, and that push counts against the push budget.
6. Last, the secret scan (gitleaks) passes on exactly the commits it sends; without gitleaks nothing is pushed.

`--public` is the developer's own action: it pushes this once whatever the backup policy, and a branch that reads as
a security fix. devkit refuses it from a crew member's lane, and the guard asks the main session for the developer's
grant (`grant backup <project>`).

Each push is audited as `git.backup` (branch, remote, commit range) and each skip as `git.backup-skipped` with its
reason, which the branch map shows as the reason a branch is not backed up. `dev sync` runs the same backup
by itself (below), but only for commits older than `backup.after_minutes`.

```text
✓ feat/report → origin: 2 commits (5f0c1d2e3a4b..9a8b7c6d5e4f) · ci.yml runs on pull requests and pushes to main
! feat/x not backed up: a push to this branch starts CI (ci.yml runs on every push)
    dev backup --accept-ci feat/x   (push anyway: one CI run)
    add feat/x (or a pattern for it) to `branches-ignore` in ci.yml
```

Exit codes: `1` when a branch was not backed up because a check failed, `0` otherwise (pushed, or nothing to back
up).

### `dev backup policy [on|off|ask] [-p <project>] --why "…" [--dry-run] [--json]`

Shows or sets the project's backup policy (the repository you are in, or `-p <owner/repo>`). It is kept in the
`backup_policy:` record of the project's handoff note (policy, why, who, when), so every machine and the team follow
it, and audited as `dev.policy` (target `backup/<project>`). `dev status`, `dev branch ls`, the handoff note's branch block and the
messages of `dev backup` show it.

| Policy | What happens |
| --- | --- |
| `on` | `dev backup` and the hourly `dev sync` push the project's branches, with every check of `dev backup`. The default for a private repository |
| `off` | No backup pushes them; `dev backup --public` still can, once |
| `ask` | `dev backup`, run by the developer in their own terminal (stdin a terminal, no agent among its parents), asks once and records the answer as `on` or `off`. `dev sync` and an agent's call are never asked and push nothing. The default for a public repository, and for one whose visibility devkit cannot read |

Setting it needs `--why` and is the developer's own action, like `--public`: devkit refuses it from a crew member's
lane, and the guard asks the main session for the developer's grant. Without a value it only shows the policy
(`--json` too); `--dry-run` writes nothing.

### `dev commit -m "type(scope): summary" [options] [<paths>…]`

Commits the staged changes, or `-a` every tracked change, or the listed paths. The header must be a Conventional
Commit with a known type, in the imperative mood, without a final period, and at most 72 characters. Commits on the
default branch and on non-conforming branches are refused; AI attribution lines are removed; the guard's secret scan
runs.

| Option | Effect |
| --- | --- |
| `-m "…"` | The header; more `-m` options add paragraphs |
| `--why "…"` | The first paragraph of the body: why the change is needed ([the commit template](git-and-github.md#commits)) |
| `--body "…"` | A body paragraph, repeatable: what changed (after `--why`), or the reason without `--why` |
| `--verified "…"` | The last paragraph of the body, `Verified: …`: how you verified the change |
| `--refs "…"` | The `Refs:` trailer: `#12`, `owner/repo#12`, a URL, or a ticket key; repeat it for more. Default: the ticket of the task started on this branch. A devkit task id is refused in it, as anywhere in the message |
| `--breaking "…"` | The `BREAKING CHANGE:` trailer: what breaks, and how to move to the new behavior |
| `-a`, `--all` | Stage every tracked change first |
| `--amend` | Amend HEAD (keeps its message without `-m`); warns when HEAD was already pushed |
| `--wip [-m "what"]` | Save everything, untracked files too, as a `wip:` commit instead of a stash |
| `--unwip` | Undo the WIP commit at HEAD; its changes come back uncommitted |

### `dev rebase [--base REF] [--continue | --abort | --skip]`

Fetches and rebases the current branch on `origin/<default>` (or `--base`). It needs a clean tree, lists the files of
a conflict, and on the default branch only fast-forwards.

### `dev push [--force-with-lease] [--ci-passed] [--allow-behind] [--dry-run]`

Pushes the current branch once and sets its upstream, after checking the branch (not the default one, a valid name),
a fresh fetch, that `origin` has nothing you lack, no WIP or fixup commits, a first push that is not behind
`origin/<default>`, the local CI evidence, and the guard's push budget.

| Option | Effect |
| --- | --- |
| `--force-with-lease` | After a rebase or amend, on your own branch; refused when it would drop someone else's commits |
| `--ci-passed` | Attest that the full local suite passed on these files (instead of `dev ci --run`) |
| `--allow-behind` | A first push even though `origin/<default>` moved on |
| `--dry-run` | What would be pushed, the evidence, and the pushes this hour |

### `dev pr create | view | ls | checks | merge | ready | comment | review | diff`

`[n]` is a PR number; without it, the PR of the current branch.

| Command | Effect |
| --- | --- |
| `dev pr create [--title T] [--base B] [--draft \| --ready] [--fill \| --body-file F \| --edit] [--ci-passed] [--lang L] [--dry-run]` | Opens the PR of the pushed branch with a Conventional title and the body from the PR template, pre-filled with the commits, the local CI result, and the links (`--fill`, the default). Empty "What and why" or "How to verify" sections are refused unless `--draft`. It needs local CI evidence, except for a draft where no workflow runs on draft PRs (`dev pr ready` then asks for it); where one does, the refusal names it. With `delivery.draft_pr` on, the PR opens as a draft unless `--ready`. `--dry-run` prints the title, the template used, the evidence, and the body; `--body-file -` reads standard input |
| `dev pr view [n] [--body] [--json]` | One-line state: branches, checks, review, conflicts, size, author, age, address |
| `dev pr ls [--all] [--all-projects [--offline]] [--json]` | Your open PRs and those waiting for your review; `--all` adds the others; `--all-projects` covers every project |
| `dev pr checks [n] [--watch [--fail-fast] [--interval S] [--timeout MIN]] [--lines N] [--json]` | Pass, fail, and pending counts; each failing job with its trimmed log (25 lines). Exits with `0` (passed), `1` (failed), or `8` (pending). When every check was skipped (a draft's run skips the jobs with the draft gate), it reports no checks yet and exits with `8`, never passed. `--watch` waits and prints only the result; it also waits up to two minutes for the real run after `dev pr ready` |
| `dev pr merge [n] [--squash \| --merge \| --rebase] [--auto] [--keep-branch] [--dry-run]` | Merges when the checks passed and the reviews allow it, with the repository's method (checks that were all skipped have not passed: it waits for the real run); a squash commit gets the PR title and its "What and why"; then deletes the branch and updates the default branch locally. `--auto` lets GitHub merge when the checks pass |
| `dev pr ready [n] [--ci-passed] [--lang L]` | Marks a draft ready after checking its title and required sections. Marking ready starts the one CI run, so for the current branch it also needs the PR's head to be your HEAD and local CI evidence for it (`--ci-passed` attests it) |
| `dev pr comment [n] -m "…" \| --body-file F` | Comments on a PR |
| `dev pr review [n] --approve \| --request-changes \| --comment [-m "…"]` | Reviews a PR; `--request-changes` and `--comment` need a message |
| `dev pr diff [n] [--stat \| --patch \| --name-only] [--json]` | Files changed with +/−, largest first (`--stat`, the default), the full patch, or the names only |

### `dev run ls | view | rerun`

| Command | Effect |
| --- | --- |
| `dev run ls [--branch B \| --all-branches] [--limit N] [--json]` | Recent CI runs of the branch on any host (default 8) |
| `dev run view [id] [--failed] [--lines N] [--json]` | A run's failing jobs and their failing step (default: the branch's latest run); `--failed` adds the trimmed logs (30 lines). Exits with `1` when a job failed |
| `dev run rerun <id> --flaky "test and evidence" [--all-jobs]` | Re-runs the failed jobs of a run, only with a reason for a proven flaky test, which is recorded in `~/.local/state/devkit/flaky.jsonl` and the project's session log |

### `dev release [--version V] [--notes-file F] [--ci-passed] [--sign] [--draft] [--prerelease] [--dry-run]`

Tags and publishes a release from a clean default branch that is in step with `origin`, by the delivery policy:
the next CalVer version unless `--version` (required for other schemes), version files that agree, local CI evidence,
CI on GitHub not failing or running, and notes from the CHANGELOG section. It creates the annotated (or signed) tag,
pushes it, and creates the GitHub release. A project that releases with release-please or semantic-release is
refused. See [Git and GitHub › Releases](git-and-github.md#releases).

### `dev issue ls | view | create`

| Command | Effect |
| --- | --- |
| `dev issue ls [--mine] [--label L]… [--state open\|closed\|all] [--limit N] [--json]` | Issues, compact (default: 20 open) |
| `dev issue view <n> [--comments] [--json]` | One issue, with the last five comments when asked |
| `dev issue create --title "…" [--body "…" \| --body-file F] [--label L]… [--assignee A]` | Opens an issue, with attribution removed |

### `dev git <args>…` · `dev gh <args>…`

Run any git or gh command with the same rules: `dev git push` and `dev git commit` get the `dev push` and `dev commit`
checks, `dev git stash` is refused in favor of `dev commit --wip`, and `dev gh pr create`, `dev gh pr merge`, and
`dev gh release create` get the evidence, gate, and policy checks (`--admin` is refused). Everything else passes the
guard. Put git options before the subcommand after `--`, or use `dev git -C DIR`.

## Quality

Tests and unfinished work. See [Testing](testing.md).

### `dev test [run | plan | prove | audit] [-p PROJECT]`

Runs only what a change can break: the tests that failed last time first, and when they pass, the tests that the
changed files can affect (the blast radius). It finds the project's runner (pytest, unittest, Jest, Vitest, Maven,
Gradle, Go, PHPUnit, or Pest) and prints a compact result: each failure with `file:line` and its first error line, and
the path to the full log.

| Command | Effect |
| --- | --- |
| `dev test` | Last failures first; if they pass, the affected tests |
| `dev test run [<paths>…] [--failed \| --affected \| --all]` | These test files, directories, or runner ids; or only the last failures, only the affected tests, or the full suite |
| `dev test plan [--all]` | What would run and why (changed file → tests), with the time estimate; runs nothing. `--all`: the whole suite, its full-run time, and the slowest files |
| `dev test prove [<tests>…] [--runs N]` | For each new or changed test, in a temporary copy: it passes `N` times with your change (default 5) and fails with the production files restored from the base. Verdicts: proves the change, does not test the change, flaky, fails with the change, did not run without the change |
| `dev test audit [<paths>…] [--changed] [--min-severity low\|medium\|high] [--rule RULE]` | Tests that cannot fail: no or trivial assertions, empty bodies, swallowed failures, skips without a reason, sleeps, and more. `--changed`: only test files changed on this branch, staged, unstaged, or untracked. Default severity shown: medium and up. `--rule` repeats |

| Option | Effect |
| --- | --- |
| `--since REF` | Compare with `REF` instead of the merge-base with `origin/<default>` (`run`, `plan`, `prove`, `audit`) |
| `--runner NAME` | Only this runner: `pytest`, `unittest`, `jest`, `vitest`, `maven`, `gradle`, `go`, `phpunit`, `pest` |
| `-v`, `--verbose` | Stream the runner's full output (it streams to a terminal anyway) |
| `--json` | The result as JSON |
| `-p PROJECT` | Another project; put it before the subcommand: `dev test -p acme/billing plan` |

```text
  Test plan · fireflysoftwaresolutions/devkit · 135 changed file(s) against origin/main ───
  last failures: none
  .github/workflows/ci.yml  → -  CI file: every test can depend on it
  CHANGELOG.md (+43 more like it)  → -  docs: no tests
  lib/devkit/audit.py (+16 more like it)  → tests/test_ciwatch.py, tests/test_codex_plugin.py, tests/test_crew.py +20  23 test file(s) depend on it
  …
  would run: unittest · 25 file(s) · ~53.8s
```

Exit codes: `0` when the tests pass or nothing needs to run, `1` when a test fails, and `2` when a selection ran no
test at all (a mistyped path, a filter that matches nothing) or no runner fits. `dev test prove` exits `1` unless every
test proves the change; `dev test audit` exits `1` on a high finding.

### `dev stubs [<paths>…] [--staged | --since REF | --all] [--json] [-p PROJECT]`

Unfinished work: `TODO`, `FIXME`, `XXX`, `HACK`; "not implemented" in any language; placeholders (`TBD`,
`coming soon`, `lorem ipsum`, `placeholder` or `stub` stated in a comment); and Python functions whose body is only
`...` (abstract methods,
Protocols, `@overload` and `.pyi` files are fine). A line that names its task (`TODO(T-fwk6abc123)`), its issue
(`#123`), or a reason (`devkit: allow-stub <reason>`) is planned work and passes. By default it checks the uncommitted
changes against HEAD (untracked files wholly); `--staged` checks what the next commit takes; `--all` checks every
tracked file. Exit 1 when it finds something.

The same check runs in the Stop hook (the files a development turn edited) and in the guard before `git commit` and
`dev commit` (what the commit takes). `DEVKIT_ALLOW_STUBS=1` lets a commit through when the developer accepts it.

## Files

Agents and people both use these safe, token-light commands. Their output is compact and capped: every list ends with a
`+N more — narrow with …` line, and it is plain text when it is not a terminal. A refusal names the reason and the
flag that overrides it (or says there is none), and a refused batch changes nothing. The guard steers raw deletes and
overwrites to these commands (see [Guard rules › Deleting and overwriting files](guard.md#deleting-and-overwriting-files)).

### `dev files [<path>] [-d DEPTH] [--all] [--max N] [--json]`

A compact tree that respects `.gitignore`: folders first, file counts, sizes from 100 KB (files) and 1 MB (folders) up,
git markers (`M`, `??`, …), and noise folded away (build output, `.git`). In folders with more than 20 files,
extensions with at least 8 files fold into one line.

| Option | Effect |
| --- | --- |
| `-d`, `--depth N` | Levels to expand (default 2) |
| `--all` | Include gitignored files and build output |
| `--max N` | Line cap (default 80; `0` for none) |
| `--json` | Machine-readable output |

### `dev find [<pattern>] [<path>] [options]`

Finds files by name, respecting `.gitignore`, with paths relative to where you are. The pattern is a glob when it looks
like one (`*.py`) and a regular expression otherwise; it ignores case unless it has a capital letter. It uses `fd`
when installed.

| Option | Effect |
| --- | --- |
| `-e`, `--ext EXT` | An extension; repeat it |
| `-t f` · `-t d` | Files or folders only |
| `-g`, `--glob` · `--regex` | Force the pattern type |
| `-p`, `--full-path` | Match the whole path, not only the name |
| `--changed` | Only paths git reports as changed or untracked |
| `--since WHEN` | Changed within `30m`, `2h`, `2d`, `1w`, or since `YYYY-MM-DD` |
| `--size SIZE` | At least (`+1M`) or at most (`-100k`) that size |
| `--all` | Include gitignored files |
| `--max N` · `--json` | Result cap (default 60) · JSON |

### `dev grep <pattern> [<paths>…] [options]`

Searches file contents with ripgrep (a Python fallback when it is missing) and prints `file:line: text`, trimmed to
one line and capped, five matches per file by default. Case is smart unless you say otherwise.

| Option | Effect |
| --- | --- |
| `-i` · `-s` | Ignore case · case-sensitive |
| `-F` · `-w` | A literal string · whole words |
| `-g GLOB` | Include or exclude files: `-g '*.py' -g '!tests/**'` |
| `-t TYPE` | A ripgrep file type: `py`, `ts`, `java`, … |
| `-l` · `--count` | Matching files only · matching lines per file |
| `-C N` | Context lines |
| `--max N` · `--max-per-file N` | Line cap (default 60) · matches per file (default 5) |
| `--all` | Include gitignored files |
| `--ast [--lang LANG]` | Structural search with ast-grep (`$X` metavariables) |
| `--json` | JSON |

### `dev diff [REV…] [PATH…] [options]`

A token-light `git diff`: the changed files and their line counts first, then hunks with one line of context. Lock
files, minified files, snapshots, build output, and generated code fold to one line, and so does any file with more
than 300 changed lines. Outside git, or with two untracked paths, it compares the two paths directly.

| Option | Effect |
| --- | --- |
| `--staged` | Staged changes |
| `--names` | Changed paths only |
| `--word` | Word diff |
| `--syntax` | Structural diff with difftastic (`difft`) |
| `-U N` | Context lines (default 1) |
| `--max N` · `--fold N` | Total line cap (default 300) · fold files with more changed lines (default 300) |
| `--no-index` | Compare two paths directly |

### `dev rm <paths>… [flags]` · `dev rm --undo [N]` · `dev rm --list`

A safe delete. `dev rm` never unlinks: it moves paths to the macOS Trash and records each one in
`~/.local/state/devkit/trash.jsonl`, so `dev rm --undo` puts them back. The deletion of a tracked file is staged in
git, like `git rm`, and undoing it unstages it.

```text
$ dev rm README.md
✗ dev rm refused 1 of 1 path; nothing was deleted
  README.md: tracked by git (the deletion is staged, like git rm) → retry with --tracked
$ dev rm -r lib/devkit/__pycache__
trashed lib/devkit/__pycache__/ (1.7 MB) · undo: dev rm --undo
```

It refuses, unless you give the flag that matches the reason:

| Reason | Flag |
| --- | --- |
| A folder | `-r` |
| Tracked by git | `--tracked` (the deletion is staged) |
| Uncommitted changes, untracked files, gitignored local files that git cannot restore (`.env`, keys, databases), or a folder outside git in the workspace | `--discard` (it stays in the Trash) |
| A file that a running process has open, or a folder a process works in | `--force-in-use` |
| Outside `~/Development`, the scratchpad, and the temp folders | `--outside` |

Some paths have no flag, and the refusal says what to do instead: system and home folders (even when they do not
exist on this Mac), the folder you are in, the workspace root and its owner, host, and `_local` project folders, the
handoff repository and its managed folders, a repository checkout, a worktree (`dev wt rm`), a submodule, a `.git`
folder and anything inside it except a stale lock file, a folder that contains a repository, and devkit's own
folders. Gitignored build output and temp files need no flag.

| Option | Effect |
| --- | --- |
| `-f` | Skip paths that do not exist (it never overrides a safety check) |
| `--dry-run` | Show the plan, change nothing |
| `--why "…"` | A reason, kept in the manifest |
| `--undo [N]` | Restore the last `N` `dev rm` runs (default 1); a path that exists again is not overwritten |
| `--list` | Recent deletions, newest first |

### `dev mv <src>… <dest> [options]` · `dev cp <src>… <dest> [options]`

Move, rename, or copy without overwriting anything silently. With more than one source, the destination must be a
folder.

| Option | Effect |
| --- | --- |
| `-f`, `--force` | Replace what exists; the old version goes to the Trash, and `dev rm --undo` restores it |
| `--dry-run` | Show the plan, change nothing |
| `--cross` | `dev mv` only: move to another project or out of the workspace |
| `-r` | `dev cp` only: copy folders |
| `--skip-heavy` · `--include-heavy` | `dev cp -r` only: copy without, or with, `.git`, `node_modules`, and `.venv` |

- **Never clobbers.** When the destination exists, the command refuses unless you pass `-f`. A folder copied onto a
  folder replaces only the files present on both sides.
- **Keeps git right.** `dev mv` uses `git mv` for tracked files that stay in the same repository.
- **Keeps the layout.** A destination that breaks the workspace layout, a backup-style name (`*.bak`, `* copy.*`), a
  copy of a repository, or a sibling copy such as `repo-pr12` is refused; use `dev wt new <branch>` for a parallel
  checkout.
- **Protects the workspace.** `dev mv` refuses the paths that `dev rm` protects (use `dev adopt` to move a
  repository). `dev cp -r` refuses to copy `.git`, `node_modules`, or `.venv` until you choose `--skip-heavy` or
  `--include-heavy`.

## Tracking and disk

### `dev canary [--days N] [--limit N]`

The instruction-canary report: per agent, how many tracked replies in the last `N` days (default 7) started with the
`[Firefly> DevKit]` marker, then the most recent replies without it (default 20) with an excerpt. Replies that carry
the marker are also scored for the format of the status block; `dev`, `dev overview`, and `dev doctor` show that score
as `format N%`. The rate counts first replies only: a reply that carried the block after a reminder is counted
apart, and `dev doctor`, `dev overview`, and `PROJECTS.md` show it as `N corrected after a reminder`, with the rate per
surface (terminal, Claude Desktop, ChatGPT app) when sessions ran on more than one.

### `dev sync [--offline] [--scheduled] [--no-push] [--quiet]`

Rebuilds the projects map (`PROJECTS.md`, `TASKS.md`, `ACTIVITY.md`, `registry.json`), the generated blocks of the
handoff notes, this machine's presence file and the dashboard, then commits and pushes the handoff repository when
something changed. Hourly (`--scheduled`) and at the end of a session, it first backs up every project's branches
that the branch map shows as not backed up, with the checks of `dev backup` and only the commits older than
`backup.after_minutes` (default 30: a younger commit may still be amended); `backup.auto: false` turns that off, and
`--offline` or `--no-push` skip it. It backs up only the projects whose backup policy is `on` (by default a
private repository; a public one asks first, through `dev backup`), and it never asks. A push that is rejected because another machine pushed first is rebased and
tried once more; a push that still fails is recorded in the audit trail and shown in "Needs attention" until a push succeeds.

You rarely need to run it: devkit queues a background sync within seconds of every commit, push, merge, tag, PR,
release, and handoff change (see [Teams and machines › Near-real-time sync](teams.md#near-real-time-sync)), and the
`sync` job runs it every hour.

| Option | Effect |
| --- | --- |
| `--offline` | No network; cached PR data of any age |
| `--scheduled` | Background mode (the `sync` job): pull first, release expired locks, refresh PR states and sizes, rotate logs. When the `gc` or `sleep` job is not installed, it also applies the safe cleanup (with `gc.auto` on) or runs `dev sleep` once a day |
| `--no-push` | Commit but do not push |
| `--quiet` | No output |

### `dev du [<name>] [--plan] [--refresh] [--json]`

What fills your disk, why, and what you can safely free. For each project on this Mac (a footer counts the portfolio
projects that are not): the main checkout, managed worktrees, Claude Code and Codex worktrees, and copies outside the
layout (flagged `!`), each with its size, branch or PR state, changes, last activity, and its largest ignored folders
by kind (build output, dependencies, caches), found by measuring. Then Colima (its disk on the Mac, what Docker holds,
and the free space macOS has not got back), agent data and caches with whether each is safe to clear, and a plan to
free space, ranked by the space each item frees, with its safety, its risk, and its command. Nothing is deleted.

The measurement is cached with its time and shown in under 2 seconds; it is taken again when it is older than 12
hours. What it measures and what is safe to delete are in [Disk use](disk.md).

| Option | Effect |
| --- | --- |
| `<name>` | One project, by alias or part of its name, and its plan items |
| `--plan` | Only the plan to free space |
| `--refresh` | Measure again now, with fresh PR states |
| `--json` | The measurement and the numbered plan as JSON, with full values (the dashboard reads it) |

### `dev free [<n>…] [--safe] [-y]`

Frees items of the plan that `dev du` showed, by number. Without numbers it prints the plan. With them, it lists
exactly what it deletes and the space, asks `Delete it? [y/N]` once, checks safety again at that moment (changed
files, commits that are not pushed, a program working in the folder), then reports what it freed; a refusal says
why and what to do. A number always means the item you saw: if a newer measurement changed the plan, `dev free`
prints the new plan and deletes nothing. It removes worktrees and copies with `git worktree remove`, deletes only
folders git ignores, runs the tool's own command for a cache or Docker images (and says which), and never prunes
Docker volumes. Every deletion is in the audit trail.

| Option | Effect |
| --- | --- |
| `<n>…` | The plan items to free, for example `dev free 2 4` |
| `--safe` | Every item of the plan marked `safe` |
| `-y`, `--yes` | Do not ask first (for scripts); in a shell that is not a terminal, `dev free` refuses without it |

### `dev gc [<n>…] [--safe] [-y] [--apply] [--auto-only] [-v]`

The same as `dev free`, under its old name, for scripts. `--apply` runs the unattended cleanup instead: it plans with
fresh PR states, asks for confirmation in a terminal (skip it with `-y`), and then syncs. `-v` also lists what that
cleanup keeps and why. `--auto-only` applies only the actions that are safe without a person, which is what the
nightly `gc` job runs. The rules are in [Disk and cleanup](disk-and-cleanup.md).

### `dev jobs [ls | show | run | logs | journal | set | enable | disable | install]`

devkit's scheduled background work as launchd jobs: `sync`, `sleep`, `gc`, `repair`, `audit`, `doctor`, and
`lessons`. See [Scheduled jobs and the audit trail](jobs-and-audit.md).

| Command | Effect |
| --- | --- |
| `dev jobs` · `dev jobs ls` | Every job: state (`on`, `not loaded`, `off`), schedule, last run, next run, and what it does |
| `dev jobs show <job>` | The job's command, schedule, timeout, notifications, log, and its recent runs |
| `dev jobs run <job>` | Runs it now, with the same log, journal entry, audit record, and notification |
| `dev jobs logs <job> [-n N]` | The last lines of its log (default 60) |
| `dev jobs journal [--days N] [-n N]` | Past runs of every job: start, result, duration, last line |
| `dev jobs set <job> --every 30m \| --at 03:30 \| --weekly "mon 09:00" [--why "…"]` | Changes the schedule in your configuration and reloads the job |
| `dev jobs enable <job>` · `dev jobs disable <job> [--why "…"]` | Turns a job on or off |
| `dev jobs install` | Writes and loads every enabled job from the configuration, unloads the ones turned off, and replaces the old `com.devkit.sync` agent |

### `dev audit [verify | export | show <id> | stats | config] [filters] [--format F] [-o FILE]`

The audit trail: every changing `dev` command, guard refusal, blocked settings change, missed status block, handoff
write and push, settings repair, cleanup, and job run, with secrets redacted. See
[Scheduled jobs and the audit trail › The audit trail](jobs-and-audit.md#the-audit-trail).

| Command | Effect |
| --- | --- |
| `dev audit [filters]` | The matching records, newest first |
| `dev audit verify [--team]` | Checks that each record is chained to the one before it and reports the first broken link; `--team` also checks the handoff repository's copies |
| `dev audit export --format jsonl\|csv\|ocsf\|cef [-o FILE] [filters]` | The matching records in one format; `-o` writes a file only you can read, and the export itself is recorded |
| `dev audit show <id>` | One record in full, as JSON (a unique start of its id works) |
| `dev audit stats [filters]` | Record counts by category and by day |
| `dev audit config` | The audit settings in effect |

| Option | Effect |
| --- | --- |
| `--days N` | The window (default 7, unless `--since` or `--until`; fractions work) |
| `--since DATE` · `--until DATE` | From or up to a date, `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM` in local time; `--until` is included |
| `--action PREFIX` | Only actions that start with it: `guard`, `job`, `dev.push`, `handoff` |
| `--actor TEXT` | Only records whose `user@host agent` contains it |
| `-p PROJECT` · `--member NAME` | One project (`owner/repo`), or one crew member (`Tank`) |
| `--category C` | `change`, `decision`, `refusal`, `security`, `lifecycle`, `job`, `release`, `data`, or `config` |
| `--severity S` | This severity or a higher one: `info`, `notice`, `warning`, `critical` |
| `--outcome O` | `ok`, `blocked`, `failed`, or `recorded` |
| `--grep REGEX` | A regular expression over the action, target, detail, and reason |
| `--team` | Every machine, from `audit/` in the handoff repository, plus this one |
| `--format F` · `--json` | `table`, `json`, or `csv` (`--json` is `--format json`); for `export`, `jsonl`, `csv`, `ocsf`, or `cef` |
| `-o FILE`, `--out FILE` | With `export`: write to this file instead of the terminal |
| `-n N` | The newest N records (default 60) |
| `--quiet` | With `--publish`, the `audit` job's command (see [Internal commands](#internal-commands)): prints nothing |

## Environment

### `dev setup [-y] [--skip-packages]` · `dev setup prompt [--remove] [-y]` · `dev setup commit-template [--remove] [-y]`

The installation and configuration wizard. It detects the Mac, GitHub, and both agents. It asks for the development
root, the handoff repository, which agents to configure and their permission modes, your stacks, your git hosts and
the commit template, clouds, the Docker VM, automation, and the settings lock. It shows the plan and applies it only after you confirm:
the packages for your stacks, hosts, and clouds, then `dev install`, the sign-ins, and `dev doctor`. Run it again to
change your choices.

`dev setup prompt` sets up only the [prompt helper](getting-started.md#the-prompt-helper): it says what it changes,
asks once, and then shows the helper in the prompt you use. With Starship it adds a marked block to
`~/.config/starship.toml` (or `$STARSHIP_CONFIG`) after a backup, and puts the helper after your git branch. Without
Starship, `oh-my-posh`, or Powerlevel10k it turns on devkit's own zsh block in `shell/devkit.zsh`, so `~/.zshrc` stays as
it is. For `oh-my-posh` and Powerlevel10k it points you to the snippet for your theme. A no is remembered: `dev install`
does not ask again. The yes must be yours, typed in your own terminal: in an agent's call, neither `-y` nor a piped
answer adds the helper, and nothing changes (exit code `2`). `--remove` needs no answer.

`dev setup commit-template` asks again whether new commit messages open with
[devkit's commit template](git-and-github.md#the-template-in-your-editor), even after a no. A yes sets
`commit.template` in the own git config of each repository that has none; repositories with a template of their own
keep it. `--remove` takes devkit's template out of every repository and records a no, so `dev install` leaves them
alone.

| Option | Effect |
| --- | --- |
| `-y`, `--yes` | Accept every default, without questions |
| `--skip-packages` | Do not install Homebrew or npm packages |
| `prompt` | Only the prompt helper |
| `--remove` | With `prompt`: take the helper out again (the Starship block too, after a backup) |

### `dev doctor`

Checks the toolchain, GitHub sign-in and git identity, the workspace (PATH, shell, config, handoff repository with its
push state, layout), the generated instructions and skills, Claude Code (permission mode, attribution, hooks,
plugins, MCP servers), Codex (approval and sandbox, hook trust, profile models, MCP servers, plugins), both agents'
sign-in and quota, the devkit plugin (validators, and whether each agent's installed copy is current), whether every
required skill is installed for both agents, project settings that switch devkit off, sessions that ran with devkit
in the last 7 days, each git host and cloud you chose (its CLI installed and signed in, checked locally), Docker,
every enabled scheduled job (loaded, and its last run passed), the desktop apps (Claude Desktop's Code tab runs its
bundled Claude Code on the same `~/.claude` settings, hooks, plugin, and skills; the ChatGPT app's bundled Codex sees
the same `~/.codex` configuration and has the same plugins enabled), the instruction canary (fails below 90% over 7
days, per surface, with corrected misses counted apart), and free disk. Each failure prints its fix. Exits with `1` if any
check fails.

### `dev agents [status | test | rules] [claude | codex | all] [--json] [--model MODEL] [-v] [--section SECTION]`

`dev agents` reports, for Claude Code and Codex: version, desktop app, sign-in method, plan, account, login expiry,
quota and the last self-test. `--json` prints the same as JSON.

`dev agents rules` prints devkit's global rules for agents as `dev install` writes them into `~/.claude/CLAUDE.md`
and `~/.codex/AGENTS.md`. `--section` prints one section, by its number (`0` to `12`) or words from its title, for
example `dev agents rules --section "status block"`. The Claude Desktop extension's `devkit_rules` tool returns the
same text.

`dev agents test [claude|codex]` runs a short real session in a scratch repository and checks the integration from
recorded evidence: the session ran, the SessionStart hook recorded it, the guard blocked a tool-prefixed branch, the
reply carried the status block (and its format score), the global instructions were loaded, and the devkit plugin
skills are visible. `--model` picks the model for that session, and `-v` prints the agent's reply.

### `dev desktop pack | status [--no-validate] [--json]`

The devkit extension for Claude Desktop's chat and Cowork (see [Agents › The devkit
extension](agents.md#the-devkit-extension)).

| Command | Effect |
| --- | --- |
| `dev desktop status [--json]` | The installed version, whether it is enabled, since when, the **devkit folder** setting, and whether its launcher finds your devkit checkout |
| `dev desktop pack [--no-validate]` | Builds the bundle in `dist/desktop/devkit-<version>.mcpb` and validates it with the `mcpb` CLI; `--no-validate` skips that check (no `npx` download). `dev install --only desktop` packs it and opens it in Claude Desktop |

### `dev mcp`

Serves devkit's tools over MCP (the Model Context Protocol) on standard input and output. The Claude Desktop
extension's launcher runs it; you do not run it yourself. Each tool runs a `dev` command without a shell and with a
timeout.

### `dev config [diff | repair | show | edit | validate | set | lock | unlock | status] [--check] [--quiet]`

Previews and enforces the agent settings that devkit manages. See [Agents](agents.md).

| Command | Effect |
| --- | --- |
| `dev config` · `dev config diff` | A report of every difference between devkit's configuration and what is installed (Claude Code, Codex, instructions, skills, git). Each one says what it is (`+` new in this devkit version, not installed yet; `~` changed outside devkit; `−` removed outside devkit), shows its value in readable fields (a hook: what it does, its event, matcher, command and time limit), and names the fix. Exits with `0`; a broken config file exits with `2` |
| `dev config --check` | One line and exit `1` when a setting differs, nothing and exit `0` otherwise: for scripts and `dev doctor` |
| `dev config repair [--quiet]` | Restores the managed keys and logs what changed. Session start runs it in the background |
| `dev config show` | The location of the config file and of devkit, and the effective configuration as JSON |
| `dev config edit` | Opens `~/.config/devkit/config.json` in your editor, then validates it |
| `dev config validate` | Checks the config file and devkit's own configuration files |
| `dev config set <key> <value>` | Sets one key, such as `dev config set progress.max_parallel 6`. The value is read as JSON when it parses (`6`, `true`, `{"70": 0.5}`), otherwise as text. Only the developer runs it, in a terminal; an agent, a hook, or a job is refused. The value is checked against [Configuration](configuration.md), written atomically, and audited as `dev.config.set` |
| `dev config lock` · `unlock` | Installs or removes a Claude Code managed-settings file (asks for your administrator password) that user settings cannot override |
| `dev config status` | The lock state and the most recent configuration events |

### `dev guard check "<command>" | --write PATH [--agent claude|codex] [--cwd DIR]`

Shows what the guard decides for a shell command or a file write, without running anything. Quote a command that has
its own options. Exits with `1` when the guard would deny or ask.

```bash
dev guard check "git push --force"
dev guard check --write ~/Desktop/notes.md --agent codex
```

### `dev plugin validate`

Validates the devkit plugin with Claude Code's validator and with Codex's own manifest parser (version, display name,
logo and skills).

### `dev install [--only STEPS | --version VERSION | --unverified --why "…"]`

Pins devkit, then applies the configuration; safe to run again. It first makes a read-only, checksummed copy of the
checkout's commit in `~/.local/share/devkit/releases/<version>-<commit>/` and runs every step from that copy, so the
hooks, the guard, the status line, `~/.local/bin/dev`, the scheduled jobs, and the Claude Desktop extension run it,
never the checkout. It pins only a release (a `vYY.MM.PATCH` tag that GitHub has on the same commit) or a commit whose
`bin/dev ci --run` passed on this Mac, and refuses tracked files with uncommitted changes and code that does not
compile. It keeps the newest three copies and any copy in use. Run it yourself, in a terminal: the guard refuses
`dev install`, `dev update`, and `install.sh` from an agent. See
[Getting started › The pinned copy](getting-started.md#the-pinned-copy).

| Option | Effect |
| --- | --- |
| `--only STEPS` | Re-applies only these steps from the installed copy, without pinning. `STEPS` is a comma-separated list of `config,tools,cli,git,handoff,instructions,skills,claude,codex,desktop,docker,docs,launchd` |
| `--version VERSION` | Switches to a copy pinned by an earlier install, for a rollback: `26.11.2`, or `26.11.2-<commit>`. It checks the copy against its checksums first |
| `--unverified --why "…"` | Pins the checkout's commit although it is neither a release nor tested; the reason is recorded in the audit trail |
| `--commit-template yes\|no` | The answer `dev setup` gives for the [commit template](git-and-github.md#the-template-in-your-editor): the git step records it, and `yes` sets `commit.template` in each repository that has none |

The `tools` step installs only the Homebrew and npm packages that are missing for your stacks, git hosts, and clouds
(and the Azure DevOps CLI extension), unless `packages.auto` is off. The `desktop` step builds the Claude Desktop
extension and opens its installer when it is missing or out of date. The `launchd` step installs the
[scheduled jobs](jobs-and-audit.md). See
[Getting started › What dev install configures](getting-started.md#what-dev-install-configures).

### `dev update`

Updates the devkit checkout, then runs `dev install`, which pins the new commit. It always updates the checkout,
even when `dev` runs from the pinned copy. What it does depends on where the checkout is:

| The checkout is | `dev update` |
| --- | --- |
| At a release tag (how the installer leaves it) | Fetches the tags and checks out the newest release, the highest `vYY.MM.PATCH` compared as numbers (`v26.11.10` is newer than `v26.11.2`); at the newest release already, it says so and stops without `dev install` |
| On a branch | `git pull --ff-only`; `dev install` then pins the new commit only after `bin/dev ci --run` passes for it |
| At a commit that is neither a release nor a branch | Refuses, and says how to check out a release or a branch |
| Changed: tracked files have local changes | Refuses, and changes nothing; commit or discard the changes first |

### `dev docker up | down | status | trim | prune | autostart-on | autostart-off`

| Action | Effect |
| --- | --- |
| `up` | Start Colima with the configured resources (created on the first run) and print the Docker server summary |
| `down` | `colima stop` |
| `status` | `colima status` and `docker system df` |
| `trim` | Return the space freed inside Colima's virtual machine to macOS: `fstrim` in each running profile, then how much macOS got back. Deletes nothing (see [Disk use › Colima's disk](disk.md#colimas-disk)) |
| `prune` | `docker system prune -f` and `docker builder prune -f` (never volumes) |
| `autostart-on` · `autostart-off` | Start Colima at login with `brew services` (or stop doing so) |

### `dev version`

Version, install path, and copyright.

## Shell integration

`dev install` makes `~/.zshrc` source `shell/devkit.zsh`, which adds:

| Function | Effect |
| --- | --- |
| `dcd <name>` | `cd` into a project by its alias, id, or part of its name (when two or more match, `dev` lists them) |
| `dwt <branch> [--base REF]` | Create or reuse a managed worktree and `cd` into it |

It also puts `~/.local/bin` on the PATH, sets the Docker and telemetry environment variables, and loads zsh completion
for `dev` and `dcd`: the commands, project aliases, worktree branches, the verbs of `dev task`, `dev progress`, `dev crew`,
and `dev agents` with their main options, the options of `dev dashboard` and `dev install` (its steps and the pinned
copies for `--version`), and the Docker actions.

Once `dev setup prompt` turned it on, it also draws the [prompt helper](getting-started.md#the-prompt-helper): before
each prompt, `_devkit_prompt` sets `DEVKIT_PROMPT` and `DEVKIT_PROMPT_ASK` inside devkit projects and unsets them
elsewhere. Because the shell sources the cache, it reads it only when the cache is a regular file of yours, not a
symlink, in a folder of yours, and no one else can write either; and only digits from it reach zsh arithmetic.

### `dev prompt [refresh]`

What the prompt helper shows in the current folder, for example `✦ devkit · 6 working · checks ✓  ! 1 question for
you`, and how it reaches your prompt (Starship, zsh, `oh-my-posh`, or Powerlevel10k). The helper reads a cache that
devkit's hooks and commands keep current (`dev status`, `dev alias`, `dev ci watch`), so a prompt runs no program and
costs well under 5 ms. `dev prompt refresh` writes that cache for every project on this Mac at once.

## Internal commands

`dev hook <event> --agent claude|codex` is the hook entry point that Claude Code and Codex call (see
[Agents › Hooks](agents.md#hooks)). `dev __complete <projects|aliases|branches|commands>` feeds shell completion.
`dev jobs exec <job>` is what launchd runs for a scheduled job, `dev audit --publish` is the `audit` job's command,
and `dev sync --delay N` is the queued near-real-time sync.

See also [Concepts](concepts.md), [Configuration](configuration.md), and [Teams and machines](teams.md).
