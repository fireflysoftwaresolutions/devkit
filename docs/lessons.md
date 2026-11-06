# Lessons

A mistake is cheap the first time and expensive every time after. devkit keeps **lessons**: short, blameless records
of what went wrong, why, and what stops it from happening again. Every session starts with the lessons that apply,
repeats are counted, recurring lessons are turned into automation, and lessons that no longer help fade away at
night, like a brain consolidating what matters while it sleeps.

The `learning-loop` skill teaches both agents this loop, and the global instructions require it.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/diagrams/lesson-lifecycle-dark.svg">
  <img src="images/diagrams/lesson-lifecycle-light.svg" alt="The life of a lesson: signals such as fix and revert commits, repeated guard refusals, missed status blocks, bouncing tasks, and corrections from the developer prompt a lesson; dev lesson add captures it in lessons/<scope>/ in the handoff repository; every session recalls the open lessons that apply as known pitfalls; dev lesson recur counts a repeat, and a recurring lesson is automated with dev lesson prevent (a test, lint rule, guard rule, required skill, or CI check); every night dev sleep merges near-duplicates and forgets lessons that no longer help into lessons/archive/, from where dev lesson restore brings one back." width="100%">
</picture>

## When to record a lesson

Record one when something went wrong that could happen again:

- you fixed a bug, or a change was reverted;
- CI failed for a reason the local run did not catch;
- the guard refused the same kind of command more than once;
- a task went back to "doing" more than once;
- the developer corrected the agent, for example with "that's wrong" or "you forgot the migration."

Skip typos and one-off hiccups of the environment. Apply the durable bar first: if this lesson disappeared, would
someone reading the final code still repeat the mistake? If the code or its test already holds the knowledge, a lesson
adds nothing.

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
| `correction` | A prompt that corrects the agent, such as one that starts with `no,`, `that's wrong`, `you forgot`, or `otra vez`, queued by the prompt hook |
| `task` | A work item that was reopened or unblocked more than once |

Corrections are only queued as signals; devkit never writes a lesson by itself.

## Write it

```bash
dev lesson add "Pushed after a piped test run hid the failure" \
  --kind process --severity high \
  --happened "npm test | tail -5 && git push pushed a red build" \
  --cause "the && saw the exit code of tail, not of the tests" \
  --fix "ran the suite again without the pipe" \
  --prevent "never gate a commit or push on a pipeline without set -o pipefail" \
  --detect "CI fails right after a push that passed locally" \
  --evidence "commit d75a9f9"
```

```text
✓ L-fwqyx0ywj1x recorded in acme/billing → ~/Development/.handoff/lessons/acme/billing/L-fwqyx0ywj1x.md
  automate it when you can: dev lesson prevent L-fwqyx0ywj1x --by test|lint|guard|skill|ci --ref … --why …
```

- **The root cause and the prevention are required.** Ask "why" until the answer is something you can change: a
  missing test, an unclear rule, a tool that allows the wrong thing. The prevention is a rule someone can follow
  tomorrow, stated as an action.
- **Choose the scope.** The project you are in (the default, or `-p`), an initiative (`-i <slug>`), or `--global` for
  mistakes any project can make.
- **Kinds:** `bug`, `mistake` (the default), `process`, `tooling`, `security`, `performance`, `design`. **Severity:**
  `low`, `medium` (the default), `high`.
- **Optional:** `--happened`, `--fix`, `--detect`, `--evidence` (repeat it: commits, PRs, CI runs, tasks), `--tag`,
  `--why`, and `--retire-when` for an outside change that makes the lesson obsolete, such as a library version
  or an upstream fix.
- **Never put secrets or personal data in a lesson.**

A lesson is a Markdown file in the handoff repository, `lessons/<project id>/`, `lessons/initiatives/<slug>/`, or
`lessons/global/`, with its fields in a front matter block, the sections What happened, Root cause, Fix, Prevention,
Detection, and Evidence, and a changelog. Every change bumps its version and makes one commit; the guard refuses
direct edits of `lessons/`.

## Recall

At session start, the context lists up to four **known pitfalls**: the open lessons of the project, its initiatives,
and the global ones, strongest first, each with its prevention rule:

```text
Known pitfalls (lessons from earlier mistakes; follow their prevention, and `dev lesson recur <id> --evidence …` if one happens again):
- L-fwqyx0ywj1x (acme/billing, high, ×2): Pushed after a piped test run hid the failure — never gate a commit or push on a pipeline without set -o pipefail
```

Before you change an area, search what is known about it:

```bash
dev lesson ls                     # this project's lessons, its initiatives', and the global ones
dev lesson ls --search "release"  # by words in the title or any section
dev lesson ls --all               # every scope, retired lessons included
dev lesson show L-fwqyx0ywj1x     # all sections and the history (a unique part of the id or title works too)
```

## Recur and prevent

When a known pitfall happens again, count it:

```bash
dev lesson recur L-fwqyx0ywj1x --evidence "CI run 4711"
```

A lesson that happened twice is a candidate for automation, and `recur` says so. A lesson that is only text gets
forgotten; prefer, in this order, a test, a lint, type, or schema check, a guard rule, a required-skill rule, a CI
check, and only then a written rule. Record what prevents it:

```bash
dev lesson prevent L-fwqyx0ywj1x --by guard --ref "guard.PIPE_GATE" --why "the guard refuses a commit or push chained after a pipe"
```

`--by` is one of `test`, `lint`, `type`, `guard`, `skill`, `ci`, `rule`, or `doc`, and `--ref` names the test, rule, or
file. A prevented lesson leaves the session context, because the automation now remembers it, and stays searchable.
That guard rule is real: it was devkit's first lesson turned into automation (see
[Guard rules › Git](guard.md#git)).

`dev lesson retire <id> --why "…"` marks a lesson that no longer applies. `dev lesson review` lists the lessons that
recurred and are not prevented (automate them now) and the open lessons not seen for four months (retire them or
confirm them); the `lessons` job runs it every Friday and notifies you of the result.

## Use and strength

devkit counts how each lesson is used, per machine: when a session shows it, when a reply names its id (the agent
applied it), when it recurs, and when someone looks it up. Each machine publishes its counts to
`lessons/usage/<user>@<host>.json` when it sleeps, and the team's counts are summed.

A lesson's **strength** combines its severity, how often it happened, and how much it is used, and halves every 45
days without use. A lesson that sessions show again and again but nobody applies loses strength too: it is not
helping. Strength decides which lessons the session context shows and which ones sleep forgets.

## Sleep

`dev sleep` runs every night at 03:30 as the `sleep` job (see [Scheduled jobs and the audit trail](jobs-and-audit.md)).
It consolidates the lessons of every scope:

| Step | What happens |
| --- | --- |
| Merge | Two lessons that are at least 60% alike (in the same scope, or a project lesson that repeats a global one) become one. A global lesson wins over a project lesson, and otherwise the stronger one wins; it keeps the evidence of both and the sum of their occurrences |
| Forget | A lesson retired more than 30 days ago, a prevented lesson idle for 90 days, or an open lesson idle for 180 days, once it has grown weak, moves to `lessons/archive/<scope>/` |
| Cap | Each scope keeps at most 25 active lessons; the weakest go to the archive first |
| Promote | The same pattern in two or more projects is proposed as one global lesson (a proposal only) |

Nothing is deleted: git keeps every version, and `dev lesson restore <id> --why "…"` brings an archived lesson back as
open. `dev sleep --dry-run` shows what the next night would do.

Sleep also tidies devkit's own records, so local state does not grow forever: timing records older than three months,
progress runs older than 14 days, session records and instruction-load logs older than 30 days, CI status caches older
than seven days, the presence files of machines quiet for 14 days, the hooks log once it passes 5 MB (the newest 1 MB
is kept), and `dev rm` manifest entries older than 90 days. Only devkit's records and caches are trimmed, never your
files.

See also [Commands › Lessons](commands.md#lessons), [Skills and plugins](skills-and-plugins.md), and
[Teams and machines](teams.md).
