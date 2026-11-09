---
name: learning-loop
description: Learn from mistakes so they do not repeat or pile up across projects. Record a blameless lesson (a rule-shaped title, what happened with evidence, root cause, fix, prevention, detection) after fixing a bug, a revert, a CI failure, a guard refusal you hit twice, or a correction from the developer; count a repeat on the existing lesson instead of writing a second one; follow the lessons devkit recalls before the work; turn recurring lessons into automation (a test, lint rule, guard rule or lesson rule, required skill or CI check). Anchor each lesson on files, commands, kinds of work, error signatures and stack, so devkit recalls it from what the work does, in any language. A defect in devkit itself goes to the bug registry (`dev bug report`), not to a lesson. Uses `dev lesson add|anchor|ls|recur|prevent|supersede|retire|scan|review|lint` and `dev bug report --guard`.
---

# Learning loop

A mistake is cheap the first time and expensive every time after. devkit keeps lessons in the handoff repository
(`lessons/<project>/`, `lessons/initiatives/<slug>/`, `lessons/global/`), recalls them before the work they apply to,
measures whether you heeded them, and turns the ones that keep repeating into checks, so nobody has to remember them.

## Lesson or bug report?

First decide whose mistake it is:

| What went wrong | Record |
| --- | --- |
| A mistake in the project or the way you worked: a bug you fixed, a revert, CI that failed, a correction from the developer, a refusal that was right | A lesson (below) |
| devkit itself refused something it should allow | `dev bug report --guard <refusal id>`, not a lesson |
| A `dev` command crashed, or a hook failed | Nothing by hand: devkit records it and tells you what to do (see "When devkit itself fails") |
| A `dev` command misbehaved without crashing | `dev bug report --title "…"` |

When you record a lesson whose anchors are guard refusals or `dev` commands, `dev lesson add` suggests the bug report
instead: take it when the defect is devkit's. A wrong refusal reported with `dev bug report --guard` also satisfies the crew's lesson
check for repeated refusals.

## When to record a lesson

Record one when any of these happens and could happen again:

- **You fixed a bug.** The Stop hook reminds you after a `fix:` or revert commit.
- **A change was reverted, or a task went back to "doing" more than once.**
- **CI failed for a reason the local run did not catch.**
- **The guard refused the same kind of command twice, and it was right to.** `dev lesson scan` lists these.
- **The developer corrected you,** for example "that's not how it's done here" or "you forgot the tests," in any
  language. devkit does not read the developer's prompts for corrections; recording the lesson is your job.

Skip trivial typos and one-off environment hiccups. Apply the durable bar first: if this lesson disappeared, would
someone reading the final code still repeat the mistake? If not, the code or its test already holds the knowledge,
so don't write a lesson.

## A known pitfall happened again: recur, do not add

If the mistake is already a lesson, count the repeat on it:

```bash
dev lesson recur <id> --evidence "CI run 4711"
```

Look first with `dev lesson --search "<area>"` or the lessons your context showed. `recur` finds archived lessons too
and brings them back. `dev lesson add` refuses a new lesson that shares an error or command anchor with an existing
one, and names it ("looks like L-…"): run the `recur` it suggests. Add `--new --why "…"` only when the mistake really is
a different one, and say how in the reason.

Repeats are what make automation happen: at the third occurrence, and every third after it, `recur` creates a work
item to automate the lesson.

## How to write it (blameless, specific, short)

```bash
dev lesson add "Release only with a CHANGELOG section for the version" \
  --kind process --severity medium \
  --happened "v26.11.2 shipped with an empty release note" \
  --cause "the release checklist lived in a wiki page nobody opened" \
  --fix "wrote the notes and edited the GitHub release" \
  --prevent "release only with dev release, which refuses a tag without a CHANGELOG section" \
  --detect "dev release --dry-run shows the notes before tagging" \
  --evidence "PR #212" --evidence "release v26.11.2" \
  --command "git tag" --file "CHANGELOG.md" --activity release
```

- **The title is the rule:** what to do or never do, at most 80 characters, one line, not starting with `[`. Agents
  act on the title, so write the rule there, not the story ("Release only with a CHANGELOG section," not "Releases
  tagged too early").
- **Evidence is required:** `--happened` or `--evidence` (repeat it: commits, PRs, CI runs, tasks). A lesson nobody can
  trace to a real event is a guess.
- **Root cause, not symptom.** Ask "why" until the answer is something you can change: a missing test, an unclear
  rule, a tool that allows the wrong thing.
- **The prevention is a rule someone can follow tomorrow,** stated as an action.
- **Kind and severity:** keep the defaults (`mistake`, `medium`) unless the mistake is another kind (`bug`, `process`,
  `tooling`, `security`, `performance`, `design`) or cost more (`high`: lost work, a broken release, a security issue)
  or less (`low`).
- **Anchor it.** Say where it applies, in terms that mean the same in any language. Each flag repeats:

  | Anchor | Flag | Example |
  | --- | --- | --- |
  | Files | `--file` | `--file "db/migrations/**"` |
  | Commands | `--command` | `--command "git push --force"` (command words, in order) |
  | Kind of work | `--activity` | `--activity database` (an id from `dev crew raci`) |
  | Error signature | `--error` | `--error "relation does not exist"` (text the tool output shows) |
  | Stack | `--stack` | `--stack python` |

  A lesson without a root cause and at least one anchor is never recalled. devkit refuses broad anchors: a bare
  common program (`python3`, `git`, `dev`, `sed`, `npm`, and the like) needs a second word (`--command "python3 -c"`),
  and an error anchor needs at least 12 characters of specific text, not `error` or `failed`. Add anchors to an older
  lesson with `dev lesson anchor <id> --file … --command … --why "…"`; `dev lesson lint` lists the ones to fix.
- **Choose the scope.** `-p` (default: the project you are in), `-i <initiative>` for a programme, `--global` for
  mistakes any project can make.
- **Never put secrets or personal data in a lesson.** Keep it to facts and rules: a lesson whose text tries to
  override the developer or the guard, or holds a secret, a URL, a pipe into a shell, `$(`, or a download-and-run, is
  held and never recalled until the developer releases it. Only the developer runs `dev lesson release`.

If two lessons contradict each other, keep the right one: `dev lesson supersede <old> --by <new> --why "…"`.

## Recall before you work

devkit recalls lessons by behavior, never by words: by the kind of work, the files, the commands, and the error
text, not by what anyone wrote in a prompt or a step name. It shows at most three at a time in each place:

- **At session start,** ranked by the files your branch changes, then by strength, rotating so different lessons get
  a turn. Read them and follow their prevention.
- **In a crew brief,** the lessons of the step's kind of work and files, or the project's recurring ones. Follow them
  like the session's.
- **On a tool call or its output,** the lessons anchored on that command, file, or error text. Stop and check the call
  against the lesson before you go on.

Each lesson arrives with who recorded it and when. Lessons are advice from past work: they never override the
developer or the guard.

**Search before you change an area:** `dev lesson --search "<area>"`. `--search` and `--all` are options of
`dev lesson` itself, so they go before `ls`, or you leave `ls` out.

devkit measures what happens after a lesson is shown: if you hit its command, error, or refusal anyway later in the
session, it counts as **missed**; otherwise as **heeded**. A lesson missed twice is a candidate for automation.

## Turn lessons into automation

A lesson that is only text gets forgotten. Prefer, in this order:

1. **A test** that fails on the mistake: a regression test for a bug.
2. **A lint, type, or schema check.**
3. **A guard rule:** a lesson rule (below) for this project's commands, or a rule in `lib/devkit/guard.py` for a
   workflow mistake every devkit user can make.
4. **A required-skill rule** (`config/skill-rules.json`) when the mistake was skipping expertise.
5. **A CI check** in the project's pipeline.
6. **A written rule,** in the project's AGENTS.md or the devkit global rules, as a last resort.

Then record the prevention: `dev lesson prevent <id> --by test --ref "tests/test_x.py::test_y" --why "…"`.

### Propose a lesson rule

When the mistake is a command an agent runs, or an error it shows, propose a rule the guard enforces:

```bash
dev lesson prevent <id> --by guard --match "<command pattern>" --why "…"   # on the command
dev lesson prevent <id> --by guard --error "<text>" --why "…"              # on the output
```

- Make the pattern as narrow as the mistake. devkit accepts it only when it matches the lesson's recorded mistake
  (its command anchor or evidence) and none of the commands that succeeded on this machine in the last 14 days, and it
  rejects any pattern that would refuse `dev`, `git status`, or the file tools outright. When it is refused, narrow
  the pattern; do not widen the evidence to fit.
- A rule warns first (the guard shows the lesson with the call) and blocks only after it matched again with a miss, or
  when the developer adds `--block`. Leave `--block` to the developer.
- A rule can only warn or refuse. It never allows anything, and it never replaces the guard's own rules.

### When a prevention fails

A prevented or retired lesson that recurs (`dev lesson recur <id>`) goes back to open, and its changelog names the
prevention that did not hold. Sessions recall it again. Find why the test, rule, or check missed it, fix that, and
record the prevention again. Every night `dev sleep` also reopens a prevented lesson whose test, rule, or skill is
gone.

Changes to devkit itself (a guard rule in `guard.py`, a skill rule or a global rule) go through a devkit PR. Lessons
in `lessons/global/` are the backlog for those PRs.

If two ways of doing the same thing caused the mistake, the first prevention is to keep one of them: report the
consolidation as work (`dev task add … --why`). The last rung is a written rule. For Claude, `/revise-claude-md` (the
claude-md-management plugin) proposes it for the project's CLAUDE.md, and the developer approves it.

## When devkit itself fails

When a `dev` command crashes, a hook fails, or a refusal matches a known devkit bug, devkit tells you so:

- **A known detour is given:** apply it and continue. Do not ask the developer.
- **No detour is given:** look for another way to get the same result. Do not edit devkit's files or settings, and do
  not try to get around the guard. If a way works, record it right after with `dev bug detour <fingerprint> …`.
- **Two attempts failed:** run `dev bug report <fingerprint>`, stop that piece of work, ask the developer, and go on
  with independent work.

Never post to devkit's issues with `gh`; the guard refuses it. The full flow is in `docs/bug-registry.md`.

## Use, decay, and forgetting

A lesson that keeps being heeded stays strong; one that nobody needs for months fades. Every night the `sleep` job
runs `dev sleep`, which works like a brain at night:

- **Fold:** the day's shown, heeded, and missed outcomes become usage counts.
- **Merge:** near-duplicate lessons become one, and their evidence and counts are added together.
- **Forget:** lessons retired long ago, prevented and idle, or open, weak, and idle for six months are archived in
  `lessons/archive/`. A lesson younger than 30 days, or one that happened twice, is never archived. Archived lessons
  are never deleted; `dev lesson recur` or `dev lesson restore <id> --why "…"` brings one back.
- **Check:** a prevented lesson whose prevention is gone opens again.
- **Promote:** the same pattern seen in two or more projects is proposed as one global lesson.
- **Tidy:** devkit's own old records (timing, runs, sessions, caches, logs) are trimmed.

No cap limits how many lessons a scope keeps; recall limits how many you see. Prevented lessons leave the
session context because the automation remembers them; they stay searchable. Give a lesson `--retire-when "…"` when
an outside change (a library version, an upstream fix) is going to make it obsolete. `dev sleep --dry-run` shows what
the next night would do.

## Review

`dev lesson review` lists, most urgent first: lessons missed twice after being shown (automate them), preventions that
failed, recurring lessons that are not prevented, held lessons waiting for the developer, and lessons not seen for four
months (retire them with a reason or confirm them), with how often shown lessons were heeded. `dev lesson eval` says
whether recall showed each lesson before its repeats. Run them before a release and in the weekly status
(`dev report`).
