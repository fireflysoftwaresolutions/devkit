# Progress and communication

You should never have to ask an agent how it is going. This guide explains how devkit keeps you informed: live
progress with an ETA computed from the real pace, estimates corrected by history, reminders when an agent goes quiet,
desktop notifications, timing reports, written templates for every kind of message, and status reports for the team.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/diagrams/status-flow-dark.svg">
  <img src="images/diagrams/status-flow-light.svg" alt="Progress and the status block: the agent records long work with dev progress, which shares steps, estimate, and ETA with the team through presence, dev team, and the dashboard; every final message starts with the status block; after each turn the Stop hook reads it, records the marker and the format score in the canary log, and stores the block as one compact line that dev status, dev overview, the dashboard, and the next session show." width="100%">
</picture>

## Live progress with `dev progress`

Agents run these commands; you can too. The global instructions and the `communicating` skill tell agents when.

```bash
dev progress start "Export invoices as CSV" --plan "API endpoint; UI button; tests; docs" --estimate 1h
dev progress step "API endpoint streams CSV; 12 tests pass" --next "UI button"
dev progress block "need the export permission name" --next "a decision from Finance"
dev progress done "CSV export merged"
dev progress                      # every run on this Mac, and the runs of other machines
```

`dev progress step` prints the update to post, ready to paste:

```text
**Progress · Export invoices as CSV** — 1/4 · 14m elapsed of ~1h00m · ETA ~38m
  ✓ 1. API endpoint
  ▸ 2. UI button
  ○ 3. tests
  ○ 4. docs
- Done: API endpoint streams CSV; 12 tests pass
- Next: UI button
```

| Command | What it records |
| --- | --- |
| `start "<goal>" [--steps N] [--plan "a; b; c"] [--estimate 30m]` | A run for the current session: goal, number of steps (or the named steps of `--plan`), estimate. It prints how long similar work took here |
| `step "<what finished>" [--next "…"]` | One more step done, the evidence, and what comes next |
| `block "<why>" [--next "<what is needed>"]` | The run is blocked; you get a desktop notification |
| `done "<outcome>"` | The run is finished; the actual time is recorded against the estimate, and you get a desktop notification |

A run belongs to the agent session that started it. Claude Code sessions export `DEVKIT_SESSION` at session start, so
devkit knows exactly which session runs the command. For Codex, devkit uses the most recent devkit session in the same
repository (within 12 hours); from a plain shell, the run belongs to that shell.

### How the ETA is computed

- **From the real pace.** Minutes left by pace = steps left × elapsed time ÷ steps done.
- **From the estimate.** Minutes left by estimate = the calibrated estimate − elapsed time.
- **Blended.** The ETA weighs the two, and trusts the pace more as steps complete. With no steps done it is the
  estimate; from about two thirds of the steps on, it is the pace.

### How estimates are calibrated

When a run with an estimate finishes, devkit records its actual time. For the next run in the same project, it takes
the median ratio of actual to estimated time over the finished runs of the last 60 days, and multiplies the new
estimate by it. `dev progress start` says so:

```text
✓ run started · Export invoices as CSV · estimate 1h00m
  runs here take 1.4× their estimate → plan for ~1h24m · median turn 3m, p90 11m
```

Agents are told to use that number, to re-plan at 1.5 times the estimate, and to say so.

### Where progress shows up

| Where | What you see |
| --- | --- |
| The agent's messages | The update that `dev progress step` prints, at most five lines |
| The status block | The `Progress:` bullet, while a run is active (see [Concepts › The status block](concepts.md#the-status-block)) |
| `dev progress` | Runs on this Mac, and other machines' runs from their presence |
| `dev team`, the dashboard | Each live session with its run, steps, ETA, and subagents |
| Desktop notifications | A run blocks or finishes; a long turn finishes |

Progress is published to the team in the background (at most once a minute) through a sync that updates this
machine's presence file and session line.

## Notifications

devkit sends a macOS notification when:

- a run is blocked (`dev progress block`) or done (`dev progress done`);
- an agent turn that lasted `progress.notify_after_minutes` (10) or more finishes.

Turn them off with `progress.notify: false` in [Configuration](configuration.md), or for one process with
`DEVKIT_NO_NOTIFY=1`.

## What the hooks add

| Hook | What it does |
| --- | --- |
| UserPromptSubmit | Starts the turn clock. When your prompt asks for status, devkit adds the real records to the agent's context (the session's runs with their headline, last update, and running subagents, and the CI/CD runs of the branch with their current job, step, elapsed time, and ETA) and asks for the overall status template. It understands English and Spanish phrasings such as `how is it going?`, `where are we`, `what's left`, `status?`, `ETA`, `give me an update`, `¿cómo va?`, or `¿qué falta?`. Background notifications (subagent results, system notices) are not treated as your request |
| PostToolUse | Times each tool call and records the ones slower than `progress.slow_tool_seconds` (20). After `progress.nudge_minutes` (8) without an update to you, it reminds the agent to send one and to record it with `dev progress step`, at most six times per turn |
| SubagentStart, SubagentStop | Track running subagents (shown in progress and status answers) and record how long each one took |
| Stop | Records the turn's duration and sends the long-turn notification |

The same UserPromptSubmit hook is also a safety net: a session that never ran SessionStart (a hook failure, an
untrusted Codex hook, an old client) is registered at its first prompt and receives the full devkit context then.
See [Agents › Hooks](agents.md#hooks).

## Pipelines

When the work has CI/CD pipelines, agents report them in progress updates and status answers, one line per run:

```text
CI/CD: ci #142 running · job tests (Python 3.13) step 4/7 "Unit tests" · 12m elapsed · ETA ~6m · <link>
```

`dev ci status` gives them the facts for GitHub Actions, GitLab CI, Bitbucket Pipelines, and Azure Pipelines, with
an ETA from the last successful runs, and `dev ci status --watch` follows a run to the end instead of polling (see
[Commands › dev ci status](commands.md#dev-ci-status---watch---branch-name----all-branches---all-jobs---limit-n---json)).

## Where the time goes: `dev time`

```bash
dev time                  # the last 7 days
dev time --days 30 -p acme/billing
```

It reports turn length (median, p90, longest), how accurate past estimates were and the factor applied to new ones,
subagent durations, the ten slowest commands by total time (with runs and median), and the runs in progress. The
records are in `~/.local/state/devkit/timing/YYYY-MM.jsonl`, one line per slow tool call, turn, subagent, and finished
run.

## How agents write to you

The `communicating` skill holds a template for every kind of message. Its rules apply to all of them: use American English
by default, the answer first, plain words, no filler or buzzwords, real evidence instead of claims, updates of at most
five lines, and honest estimates given as ranges.

| Template | When the agent uses it | Shape |
| --- | --- | --- |
| Kickoff | Before any task longer than a few minutes | Plan with the estimate and steps; checks, risks, what it needs from you |
| Progress update | After each step, every 8–10 minutes of tool work, when background work finishes | Headline with steps, elapsed, estimate, and ETA; done, now, next, blockers |
| Status answer | When you ask "how is it going?" or "what's left?" | A one-line verdict, a table per workstream (state, evidence, ETA), the CI/CD pipelines (state, current job and step, elapsed, ETA), what is left, risks, what it needs from you |
| Background work | When subagents, parallel sessions, or long commands start and end | What runs, what it produces, and when; then the result in one or two lines |
| Decision request | Only for decisions that are yours: scope, behavior, cost, risk, irreversible, or external effects | Context, options with gains, costs, and effort, a recommendation, and what it does if you do not answer |
| Explanation | When you need to understand something | The answer in one sentence, how it works, an example from your code, what it means for you, where to read more |
| Final report | At the end of a turn | The status block, then only what the block does not say: what changed, what is not done |
| Failure or incident | When something fails | What failed and its impact, cause, evidence, what was tried, next step |
| Pull request description | When it opens a PR | What and why, changes, how to verify, risk and rollback, links |
| Merge message | When it squash-merges | The PR title, then the what and why in one or two sentences |
| Review comments | When it reviews code | `blocking:`, `suggestion:`, `nit:`, or `question:`, then what is wrong, why, and what to do |
| Handing work to a teammate | When you want someone else to take a task | The message `dev task delegate` writes (see [Teams and machines](teams.md#hand-a-task-to-a-teammate)) |

Records have one shape each, and commands create them: plans and specs (`dev plan new --kind …`), tasks
(`dev task add --type … --why … --done …`), run steps (`dev progress start --plan …`), session lines
(`dev handoff --log`), the status block, and status reports (`dev report`). The shapes are in
[Templates](templates.md).

## Status reports

`dev report` writes a Markdown status report from the handoff repository only, so every machine produces the same
report.

```bash
dev report                          # every initiative, then the projects outside initiatives, last 7 days
dev report --days 14 -i payments    # one initiative
dev report -p acme/billing -o ~/Desktop/billing-status.md
```

It starts with a summary table (initiative, health, progress, done, in progress, blocked, due date). Then each
initiative gets its health with the reason, progress (done of total tasks), milestone, due date, owner, and member
count, the goal, what was **done this period** and by whom, what is **in progress**, what is **next**, **blockers and
risks** (blocked tasks and the bullets of the README's risks section), and the **team** (people who logged sessions,
with their session counts). Projects outside initiatives follow in one table. Tasks that were imported as already done
do not count as done this period.

Health follows plain rules, in this order, so you can always explain it:

| Health | When |
| --- | --- |
| ✅ Done | The status is `done` |
| ⏸ Paused | The status is `paused` or `archived` |
| 🔴 Off track | The status is `blocked`, or the due date has passed |
| 🟡 At risk | A task is blocked; or the due date is within 14 days and progress is below 70%; or there has been no activity for two weeks |
| 🟢 On track | None of the previous rows applies |

`dev standup` is the short version for yourself: what happened since yesterday and what is in progress.

See also [Teams and machines](teams.md), [Templates](templates.md), and [Models and tokens](models-and-tokens.md).
