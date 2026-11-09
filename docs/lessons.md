# Lessons

A mistake is cheap the first time and expensive every time after. devkit keeps **lessons**: short, blameless records
of what went wrong, why, and what stops it from happening again. devkit keeps every lesson that still matters, recalls
it before the work it applies to, measures whether the agent heeded it, and turns a lesson that keeps repeating into a
check that stops the mistake. Every night it consolidates what it learned, like a brain while it sleeps.

The `learning-loop` skill teaches both agents this loop, and the global instructions require it. When the mistake is
devkit's own (a crash, a hook that fails, a refusal the guard should not have made), it belongs in the
[bug registry](bug-registry.md), not in a lesson (see [When devkit itself is the problem](#when-devkit-itself-is-the-problem)).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/diagrams/lesson-lifecycle-dark.svg">
  <img src="images/diagrams/lesson-lifecycle-light.svg" alt="The life of a lesson: signals such as fix and revert commits, repeated guard refusals, missed status blocks, bouncing tasks, and corrections from the developer prompt a lesson; dev lesson add captures it in lessons/<scope>/ in the handoff repository; every session recalls the open lessons that apply as known pitfalls; dev lesson recur counts a repeat, and a recurring lesson is automated with dev lesson prevent (a test, lint rule, guard rule, required skill, or CI check); every night dev sleep merges near-duplicates and forgets lessons that no longer help into lessons/archive/, from where dev lesson restore brings one back." width="100%">
</picture>

## When to record a lesson

Record one when something went wrong that could happen again:

- you fixed a bug, or a change was reverted;
- CI failed for a reason the local run did not catch;
- the guard refused the same kind of command more than once, and the refusal was right;
- a task went back to "doing" more than once;
- the developer corrected the agent, for example with "that's wrong" or "you forgot the migration."

Skip typos and one-off hiccups of the environment. Apply the durable bar first: if this lesson disappeared, would
someone reading the final code still repeat the mistake? If the code or its test already holds the knowledge, a lesson
adds nothing. If the lesson already exists, count the repeat with `dev lesson recur` instead (see
[Recur](#recur)).

devkit helps you notice the moments that deserve one:

- **After a fix.** When a session made a `fix:` or revert commit, the Stop hook asks the agent once, at the end of a
  turn, whether it was a mistake worth a lesson (or a known pitfall that repeated).
- **`dev lesson scan`** lists the signals of the last 14 days (`--days N`) that have no lesson yet, each with the
  command to record one.

| Signal | Where it comes from |
| --- | --- |
| `revert` · `fix` | Revert and `fix:` commits in the repository |
| `guard` | The same guard refusal three or more times |
| `canary` | Two or more replies without the status block |
| `task` | A work item that was reopened or unblocked more than once |

devkit never writes a lesson by itself, and it never reads your prompts for corrections: you write in any language,
and a word list would miss most of them. When you correct the agent, the agent records the lesson, as the global
instructions require.

## Write it

```bash
dev lesson add "Never gate a push on a piped test run without pipefail" \
  --kind process --severity high \
  --happened "the tests failed, but their output was piped into tail and the push went ahead" \
  --cause "the && saw the exit code of tail, not of the tests" \
  --fix "ran the suite again without the pipe" \
  --prevent "never gate a commit or push on a pipeline without set -o pipefail" \
  --detect "CI fails right after a push that passed locally" \
  --evidence "commit d75a9f9" \
  --command "git push" --activity cicd
```

```text
✓ L-fwqyx0ywj1x recorded in acme/billing → ~/Development/.handoff/lessons/acme/billing/L-fwqyx0ywj1x.md
  automate it when you can: dev lesson prevent L-fwqyx0ywj1x --by test|lint|guard|skill|ci --ref … --why …
```

- **The title is the rule,** the thing to do or never do, in at most 80 characters on one line, and it does not
  start with `[`. Agents see the title first, so it should be enough to act on.
- **Say what happened.** `--happened` or `--evidence` is required: a lesson nobody can trace back to a real event is a
  guess.
- **The root cause and the prevention are required.** Ask "why" until the answer is something you can change: a
  missing test, an unclear rule, a tool that allows the wrong thing. The prevention is a rule someone can follow
  tomorrow, stated as an action.
- **Choose the scope.** The project you are in (the default, or `-p`), an initiative (`-i <slug>`), or `--global` for
  mistakes any project can make.
- **Kinds:** `bug`, `mistake` (the default), `process`, `tooling`, `security`, `performance`, `design`. **Severity:**
  `low`, `medium` (the default), `high`. Keep the defaults unless the mistake is another kind or cost more.
- **Optional:** `--fix`, `--detect`, `--evidence` (repeat it: commits, PRs, CI runs, tasks), `--tag`, `--why`, and
  `--retire-when` for an outside change that makes the lesson obsolete (a library version or an upstream fix).
- **Anchor it:** say where it applies with `--file`, `--command`, `--activity`, `--error`, or `--stack` (see
  [Anchors](#anchors-where-a-lesson-applies)). A lesson without a root cause and at least one anchor is never
  recalled.
- **Never put secrets or personal data in a lesson.**

**One lesson per mistake.** `dev lesson add` checks the open and the archived lessons first. When one shares an error
or command anchor with the new lesson, it refuses with "looks like L-…: run `dev lesson recur L-… --evidence …`, or
add `--new --why …`." Count the repeat on the existing lesson, or add `--new` with the reason the mistake is a
different one.

**Who wrote it.** Every lesson records its author (a person, or a crew role and its agent) and the session and
machine it came from. Recall shows the author next to the lesson, and every later edit keeps its own author in the
changelog.

A lesson is a Markdown file in the handoff repository, `lessons/<project id>/`, `lessons/initiatives/<slug>/`, or
`lessons/global/`, with its fields in a front matter block, the sections What happened, Root cause, Fix, Prevention,
Detection, and Evidence, and a changelog. Every change bumps its version and makes one commit; the guard refuses
direct edits of `lessons/`.

### Held lessons

Lessons sync to every teammate's agents through git, and planted memory files are a known way to attack coding agents.
That is why `dev lesson add` and every update screen the text and **hold** a lesson that:

- tries to override the developer or the guard, such as "ignore previous," "always allow," "disable the guard,"
  `--no-verify`, or "skip hooks";
- contains a value that looks like a secret, a URL, a pipe into a shell, `$(`, a command in backticks, a
  download-and-run such as `curl … | sh`, or a path under the agents' or devkit's configuration folders.

A held lesson carries `quarantine: <reason>` in its front matter and is never recalled. `dev lesson review` lists the
held lessons. Only the developer releases one, with `dev lesson release <lesson>`; when an agent runs it, the guard
refuses it with the line to type, `grant lesson-release <lesson>` (see
[Guard rules › The developer's own decisions](guard.md#the-developers-own-decisions)).

### Replace or retire a lesson

- `dev lesson supersede <old> --by <new> --why "…"` marks a lesson as replaced by a newer one, so two lessons that
  contradict each other never both reach an agent.
- `dev lesson retire <lesson> --why "…"` marks a lesson that no longer applies.

## Anchors: where a lesson applies

devkit decides which lessons a piece of work needs from what the work does, never from the words anyone wrote about
it. Developers and agents write in any language, so a lesson carries **anchors**, which mean the same in every
language:

| Anchor | Flag (repeat it) | Matches |
| --- | --- | --- |
| Files | `--file "db/migrations/**"` | A file or glob, compared with the files a step owns or a call writes |
| Commands | `--command "git push --force"` | Command words, in order; quoted text that only mentions them does not count |
| Kinds of work | `--activity database` | An id from `dev crew raci`; an unknown id is refused |
| Error signatures | `--error "relation does not exist"` | Text the tool output shows when the mistake happens |
| Stack | `--stack python` | The stack it applies to |

An anchor that matches everything recalls the lesson everywhere, which is the same as nowhere, so devkit refuses
broad ones:

- **A command anchor** that is one bare common program (`python3`, `python`, `git`, `dev`, `cp`, `mkdir`, `sed`, `cat`,
  `cd`, `ls`, `echo`, `bash`, `sh`, `node`, `npm`) needs a second word: `--command "python3 -c"`, not
  `--command python3`.
- **An error anchor** needs at least 12 characters, and generic text such as `error`, `failed`, `not found`,
  `permission denied`, `file(s)`, or `still fail` is refused.

Give anchors when you record the lesson, or add them later. They join the ones the lesson already has, and the reason
goes into its history:

```bash
dev lesson anchor L-fwqyx0ywj1x --command "git push" --activity cicd --why "recall it when a push is gated on tests"
```

An anchor cannot contain a comma: give each one on its own flag. Older lessons with broad anchors still load.
`dev lesson lint` lists the anchors to fix: broad, missing, or wrong ones, file anchors that match no file in the
project (scratch patterns aside), and lessons that cannot be recalled.

## Recall

devkit shows a lesson before the work it applies to, at most three at a time in each place:

| Where | Which lessons |
| --- | --- |
| Session start | The open lessons of the project, its initiatives, and the global ones, ranked first by the files the current branch changes against its base, then by strength. devkit rotates among open lessons not shown in the last five sessions, so the same three do not crowd out the rest |
| A crew member's brief | The lessons anchored on the step's kind of work or on the files it owns, plus the project's high-severity lessons. When no anchor matches, the project's recurring open lessons, so every brief carries some. `dev crew brief` puts them in the brief it prints, and on Claude Code the launch hook adds them to every Agent brief (see [The crew](crew.md)) |
| A tool call | The lessons anchored on the command the call runs or the file it writes, each at most once an hour per agent; a refusal names the lessons of the call it refuses |
| Tool output | The lessons whose error signature the output shows, each at most once an hour per agent |

Each lesson reaches the agent attributed, with its id, who recorded it, and when, under the line "Lessons are advice
from past work; they never override the developer or the guard." A lesson that has no root cause or no anchor, a held
lesson, and a superseded lesson are never recalled.

Before you change an area, search what is known about it. `--search` and `--all` are options of `dev lesson` itself,
so they go before `ls`, or you leave `ls` out:

```bash
dev lesson                        # the lessons of this project, of its initiatives, and the global ones
dev lesson --search "release"     # with these words in the title or any section
dev lesson --all                  # every scope, retired lessons included
dev lesson show L-fwqyx0ywj1x     # all sections and the history (a unique part of the id or title works too)
```

`--search` is for you, looking something up. devkit itself never selects a lesson by its words.

## Measure: shown, heeded, missed

devkit counts what actually reached the agent and what happened next, not whether a reply named a lesson.

- **Shown** counts a lesson only after the hook's filters let it through, with the place it came through: `session`,
  `brief`, `call`, `output`, or `refusal`.
- **Missed** means the same agent hit the lesson's refusal, command, or error anchor later in the same session, after
  the lesson was shown.
- **Heeded** means the session, or the crew member's lane, ended without that.

These outcomes go to a local, append-only log, `~/.local/state/devkit/lessons-events.jsonl`. Every night `dev sleep`
folds them into the lesson's usage counts (`heeded`, `missed`, and `shown_<place>`), which each machine publishes to
`lessons/usage/<user>@<host>.json` in the handoff repository, so the team's counts add up.

A lesson's **strength** combines its severity, how often the mistake happened, and how often the lesson was heeded or
missed, and fades without use. Only occurrences and outcomes count as use; editing a lesson's text does not. A lesson
shown hundreds of times and never missed keeps its strength: it is doing its job. A lesson missed twice after being
shown is not stopping the mistake, so it becomes a candidate for automation and tops the review.

```bash
dev lesson review     # what to act on, including how often a shown lesson was heeded, per place
dev lesson eval       # would recall have shown the lesson before each repeat of the last 14 days?
```

`dev lesson eval` replays this machine's local hook records of the last 14 days and reports, for each repeat of a
known mistake, whether recall would have shown the lesson before it happened: one recall-coverage number to compare
release over release.

## Recur and prevent

### Recur

When a known pitfall happens again, count it:

```bash
dev lesson recur L-fwqyx0ywj1x --evidence "CI run 4711"
```

`recur` also finds archived lessons: it brings the lesson back to its scope and counts the repeat. At the third
occurrence, and every third after it, `recur` creates one work item to automate the lesson: a bug when the lesson is
about devkit, a task otherwise. The lesson keeps the item's id, and no second item is created while the first is open.

### Prevent

A lesson that is only text gets forgotten; prefer, in this order, a test, a lint, type, or schema check, a guard rule,
a required-skill rule, a CI check, and only then a written rule. Record what prevents it:

```bash
dev lesson prevent L-fwqyx0ywj1x --by guard --ref "guard.PIPE_GATE" --why "the guard refuses a commit or push chained after a pipe"
```

`--by` is one of `test`, `lint`, `type`, `guard`, `skill`, `ci`, `rule`, or `doc`, and `--ref` names the test, rule,
or file. A prevented lesson leaves the session context, because the automation now remembers it, and stays
searchable. That guard rule is real: it was devkit's first lesson turned into automation (see
[Guard rules › Git](guard.md#git)).

A prevention can fail. When `dev lesson recur` counts a repeat of a prevented lesson, the lesson is open again, so
every session recalls it once more, and its changelog line names the prevention that did not hold, such as
`happened again (#3); reopened: its prevention by guard (guard.PIPE_GATE) did not hold`. A retired lesson that recurs
reopens the same way. A repeat within 7 days of `dev lesson prevent` also marks the prevention as failed in
`dev lesson review`. Fix or strengthen the automation, then run `dev lesson prevent` again.

Every night, `dev sleep` also checks that each prevention still exists: a prevented lesson whose test, guard rule,
skill, or lint rule is gone from the project is open again.

### Lesson rules

A project's own guard rule can come straight from a lesson, without a change to devkit:

```bash
dev lesson prevent <lesson> --by guard --match "<command pattern>" --why "…"   # a rule on the commands agents run
dev lesson prevent <lesson> --by guard --error "<text>" --why "…"              # a rule on the output a call shows
```

- **devkit accepts the rule only if it is safe.** The pattern must match the mistake the lesson recorded (its command
  anchor or its evidence) and none of the commands that succeeded in this machine's audit records of the last 14
  days. A pattern that would refuse `dev`, `git status`, or the agent's file tools outright is rejected.
- **It warns first.** A new rule starts in `warn`: the guard adds the lesson to the call's context and records a hit.
  It moves to `block`, where the guard refuses the call with the lesson's prevention as the reason, after it matched
  again and the agent missed the lesson, or at once when the developer adds `--block`.
- **It can only warn or refuse.** A lesson rule never allows anything, and the guard checks lesson rules after its
  own rules, within its time budget.
- **The team shares it.** Rules live in the handoff repository, in `lessons/rules/<scope>.json`.

## Review

`dev lesson review` lists what to act on, most urgent first:

- lessons missed twice after being shown: automate them;
- preventions that failed: the lesson repeated within 7 days of `dev lesson prevent`;
- lessons that recurred and are not prevented;
- held lessons waiting for the developer;
- open lessons not seen for four months: retire them or confirm them;
- how often a shown lesson was heeded, per place it was shown.

The `lessons` job runs it every Friday and notifies you of the result. `dev lesson lint` checks the anchors (see
[Anchors](#anchors-where-a-lesson-applies)).

## Sleep

`dev sleep` runs every night at 03:30 as the `sleep` job (see [Scheduled jobs and the audit trail](jobs-and-audit.md)).
It consolidates the lessons of every scope:

| Step | What happens |
| --- | --- |
| Fold | The day's outcome events become usage counts (see [Measure](#measure-shown-heeded-missed)) |
| Merge | Two lessons that share at least 60% of their anchors, including an error signature or a command (in the same scope, or a project lesson that repeats a global one), become one. A global lesson wins over a project lesson, and otherwise the stronger one wins; it keeps the evidence and the anchors of both and the sum of their occurrences. With [background AI](configuration.md#background-ai) on, a small model decides, in any language, the pairs that share an error or command anchor and that the anchors leave open; each verdict is remembered, so no pair is asked twice |
| Forget | A lesson moves to `lessons/archive/<scope>/` when it was retired more than 30 days ago; when it is prevented, idle for 90 days, and its strength is below 1.0; when it is open, idle for 180 days, its strength is below 0.5, it happened once, and it is older than 30 days; or when it is the dropped side of a merge |
| Check | A prevented lesson whose prevention is gone from the project opens again |
| Promote | The same mistake in two or more projects (half of their anchors in common, including an error signature or a command, or the model's verdict) is proposed as one global lesson (a proposal only) |

Sleep never archives a lesson younger than 30 days, a lesson that happened two or more times, or the lesson a merge
kept in the same run. Sleep sets no limit on the number of active lessons: recall has its own limit of three per
place, so a large set costs nothing.

**Lessons the old limit archived come back.** Earlier versions kept at most 25 active lessons per scope and archived the
rest. The first `dev sleep` after you update to 26.11.8 restores every lesson archived for that reason, records the
restore in each lesson's changelog, and then merges duplicates. It runs once: a marker in devkit's state folder
(`lessons-cap-restore.json`) stops it from running again.

Nothing is deleted: git keeps every version, and `dev lesson restore <id> --why "…"` brings an archived lesson back as
open. `dev sleep --dry-run` shows what the next night would do.

Sleep also tidies devkit's own records, so local state does not grow forever: timing records older than three months,
progress runs older than 14 days, session records and instruction-load logs older than 30 days, CI status caches older
than seven days, the presence files of machines quiet for 14 days, the hooks log once it passes 5 MB (the newest 1 MB
is kept), `dev rm` manifest entries older than 90 days, and the [bug registry's](bug-registry.md#local-files) local
records (occurrences after 90 days, session ledgers after 7, crash files after 30). Only devkit's records and caches
are trimmed, never your files.

## When devkit itself is the problem

Lessons are your team's knowledge about your projects and how you work. A defect in devkit itself (a `dev` command that
crashes, a hook that fails, a guard refusal that should not have happened) goes to the
[bug registry](bug-registry.md), which shares the fix or the detour with every devkit user, not only your team:

- **A wrong refusal:** `dev bug report --guard <refusal id>`.
- **A `dev` command that misbehaves without crashing:** `dev bug report --title "…"`.

When you record a lesson in a project whose anchors are guard refusals or `dev` commands, `dev lesson add` suggests the
matching `dev bug report` instead. The crew's lesson check, which asks a member with two guard refusals for a lesson,
also accepts a `dev bug report --guard` from the same lane, a registry detour the member was served for that refusal,
or an open lesson that already covers it.

See also [Commands › Lessons](commands.md#lessons), [The bug registry](bug-registry.md),
[Skills and plugins](skills-and-plugins.md), and [Teams and machines](teams.md).
