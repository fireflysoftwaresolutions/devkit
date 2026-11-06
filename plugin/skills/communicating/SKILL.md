---
name: communicating
description: How to write to people — the developer and their teammates — plainly and on time. Templates for kickoff plans with estimates, progress updates during long runs and subagents, status answers ("how is it going?"), decision requests, explanations, final and failure reports, pull request descriptions, merge messages, review comments and messages that hand work to a teammate; plus `dev progress` (live progress, ETA, team updates) and `dev task delegate`. Use at the start of non-trivial work, every few minutes of silent work, when background work starts or ends, when you need a decision, when you open or merge a PR, when work moves to someone else, and at the end.
---

# Communicating with the developer

The developer should never have to ask "how is it going?". Tell them before they wonder. Everything you write, to
them or for their team, follows the same rules:

- **American English by default.** Use US spelling and conventions in replies, PRs, commits, docs and messages. Write
  in another language only when the developer asks for it (or devkit config `language` says so), and then only for
  the content they asked for.
- **Lead with the answer.** The first line is the outcome, the verdict or the question. Details come after.
- **Write plainly.** Use short sentences, everyday words and concrete nouns ("the login page times out after 30 s",
  not "authentication flow latency issues"). Use active voice. Explain each acronym the first time you use it.
- **Cut whatever says nothing.** No buzzwords: robust, seamless, leverage, comprehensive, cutting-edge, game-changing.
  No filler ("It's worth noting that…", "In order to…"). No restating the question, and no list of technologies when
  one sentence explains the point. If a sentence would not change what the reader does or understands, delete it.
- **Use real evidence, not claims.** Quote the command and its result, the test count, the PR or the run id. Never
  say "done", "fixed" or "passing" without having run it in this session.
- **Keep it short.** Progress updates are at most 5 lines. Put long material in a file or the handoff note and link it.
- **Be honest about time.** Give estimates as ranges. When you are past an estimate, say so and give a new one.

devkit records progress for you. `dev progress` keeps the run (goal, steps, estimate, updates), computes the ETA from
your real pace, corrects estimates using this project's history, publishes the state to the team (presence, dashboard,
handoff session log) and sends a desktop notification when a run finishes or blocks. If you stay silent for too long, the
hooks remind you to give an update.

## 1. Kickoff, for any task longer than a few minutes

Before you start, run:

```bash
dev progress start "<goal>" --steps <n> --estimate <30m|2h>
```

The command prints how long similar work took here. Use that to set the estimate.

```
**Plan · <goal>** — ~<estimate> (<n> steps)
1. <step> 2. <step> 3. <step> …
Checks: <how you will verify it> · Risks: <the one or two that matter> · I need from you: <nothing | a decision on X>
```

## 2. Progress update, during the work

Send one after each finished step, every ~8–10 minutes of tool work, when a subagent or background job finishes, and
before a long wait. Record it and send the text that the command prints:

```bash
dev progress step "<what finished, with evidence>" --next "<what is next>"
```

```
**Progress · <goal>** — 3/7 · 24m of ~40m · ETA ~15m
- Done: <evidence-backed result>; <result>
- Now: <what is running> (subagent X for 6m, expected ~10m)
- Next: <next step> · Blockers: none
```

## 3. Status answer, when the developer asks "how is it going?" or "what's left?"

The prompt hook adds your real progress records to the context. Use them:

```
**Status:** <one-line verdict: on track | behind by ~X | blocked on Y>
| Workstream | State | Evidence | ETA |
|---|---|---|---|
| <item> | done / in progress / waiting / blocked | <test run, PR #, file> | <~time> |
CI/CD: <pipeline> #<n> <running|queued|✓|✗> · job <name> step <k>/<n> "<step>" · <elapsed> · ETA ~<min> · <link>
Left: <remaining steps> · Risks: <top risk> · From you: <decision or nothing>
```

When the work has pipelines (GitHub Actions, GitLab CI, Bitbucket Pipelines, Azure Pipelines), always report them:
`dev ci status` shows every run of the branch with its jobs, current step, elapsed time and an ETA from recent
successful runs. Use `dev ci status --watch` to follow a run to the end instead of polling.

## 4. Background work: subagents, parallel sessions, long commands

- **On launch, say what runs in the background.** Name it, say what it will produce and when you expect it:
  "3 agents running: A, B, C; I'll integrate as each finishes (~20m)". Then keep working on independent things.
- **On completion, relay the result in one or two lines.** Say what it found or built, how you verified it and what
  you will do with it. The developer cannot see subagent output.
- **Never poll or sleep while waiting.** Use notifications (`run_in_background`, `gh pr checks --watch`). If a job
  runs past its expected time, say so.

## 5. Decision request, for questions of consequence

Ask only when the decision is truly the developer's: product behaviour, scope, cost, risk, irreversible actions or
external effects. Everything else, decide yourself and say what you decided. Use the harness's question tool when it
has one; otherwise:

```
**Decision needed · <topic>** — <the question in one line>
Context: <2–3 lines: what we know and why it matters now>
| Option | Gains | Costs / risks | Effort |
|---|---|---|---|
| A (recommended) | … | … | ~1h |
| B | … | … | ~3h |
Recommendation: A, because <reason grounded in their goals>.
If you don't answer: I'll <safe default> / I'll wait on <the blocked part> and continue with <independent work>.
```

## 6. Explanation, when the developer needs to understand something

1. **Start with the answer in one sentence.**
2. **Explain how it works**: 3–6 numbered steps, or a small diagram when there is a flow (see the `diagrams` skill).
3. **Show an example from their code or data**, with the file and line (`path:line`).
4. **Say what it means for them**: what to do, what to avoid, what changes.
5. **Point them further**: a doc, a command or a file.

Use plain words and concrete examples. Never be condescending. Don't use walls of text or unrequested tutorials.

## 7. Final report, at the end of the turn

The message starts with the status block (global instructions §0). It is the summary, so don't repeat it. Below it,
add only what the developer needs beyond the block:

```
**[Firefly> DevKit]** billing · `feat/invoice-export` · Invoice export
- **Done:** added a CSV export of invoices to the invoice list.
- **Check:** ✓ 214 tests pass; exported October (1,204 rows) and opened it in Numbers
- **Next:** open the PR; Finance tries it on Friday.

**Changed:** `InvoiceExportController`, `InvoiceList.tsx`, a new migration for the export index.
**Not done:** XLSX export (tracked as a follow-up task).
```

Then run `dev progress done "<outcome>"`, which notifies the developer and records the actual time against the
estimate.

## 8. Failure or incident

```
**Problem · <what failed>** — impact: <what is affected>
Cause: <known | suspected: …> · Evidence: <error line, log, run id>
Done so far: <what you tried and what it showed> · Next: <the fix or the question>
```

Record it with `dev progress block "<reason>" --next "<what is needed>"`.

## 9. Pull request description

`dev pr create` fills the template (the repository's own `.github/pull_request_template.md`, otherwise devkit's) from
your commits, tasks and local CI result. You write the two parts only you can write: **What and why**, and **Risk and
rollback**.

```
Title: feat(invoices): export invoices as CSV           ← Conventional Commit, ≤ 72 chars, imperative

## What and why
Finance closes the month by copying invoices into a spreadsheet by hand. This adds a CSV export on the invoice
list, so they can download a month in one click.
## Changes
- `GET /invoices/export?month=` streams CSV (RFC 4180), paginated reads, no N+1
- Export button on the invoice list; disabled while a download runs
## How to verify
- Local CI: `./mvnw verify` → 214 tests passed; `pnpm test` → 88 passed
- Manual: open Invoices, choose October, Export → the file opens in Numbers with 1,204 rows
## Risk and rollback
Low. Read-only endpoint behind the existing `invoices:read` permission. Roll back by reverting this PR.
## Links
- Task T-fwk7a2… · Closes #142
```

## 10. Merge message (squash)

```
feat(invoices): export invoices as CSV (#151)

Finance can download a month of invoices as CSV from the invoice list instead of copying them by hand.

Closes #142
```

The title matches the PR title. The body is the "What and why" in one or two sentences. Never add co-author or AI
attribution lines.

## 11. Review comments

Say what is wrong, why it matters and what to do, in that order. Mark how much it matters:

```
blocking: `amount` is parsed as a float, so 0.1 + 0.2 rounds wrong on totals. Use BigDecimal here, as InvoiceTotal does.
suggestion: this loop calls the repository once per line (N+1). `findAllById(ids)` would make it one query.
nit: "recieve" → "receive".
question: should cancelled invoices be exported too? The ticket doesn't say.
```

## 12. Handing work to a teammate

When the developer wants someone else to take a task, run `dev task delegate <n> --to <github-login> --why "<why it
matters>"`. It assigns the work item, records the reason, writes the message below and copies it to the clipboard,
so the developer can paste it into Slack, Teams or email. Review the text before it goes out; it should read like a
person wrote it.

```
Hi @maria, could you take this task: "Export invoices as CSV"?

Why it matters: Finance spends a day each month copying invoices by hand.
Done means: the export is merged with tests, and Finance has tried it on October's data.
Deadline: November 20.
Task T-fwk7a2… in acme/billing · links: #142, plans/acme/billing/2026-11-05-invoice-export.md

To pick it up, run `dev task start T-fwk7a2… -p acme/billing`. It locks the task so nobody else starts it,
and `dev task show T-fwk7a2… -p acme/billing` shows its history. Ask me anything here.
```

To draft any other message for a teammate (a question, a status note, a request for review), use the same shape:
the ask in the first line, then why it matters, what "done" means, the deadline, where to find the context, and how
to reply. Keep it under ten lines.

## 13. Structured formats for everything you record

Each kind of record has one shape, so anyone can read anything quickly. Use the commands, which create the right
structure, instead of free text.

| Record | Command | Structure |
| --- | --- | --- |
| Plan | `dev plan new "<title>"` | Summary · Context · Goals · Non-goals · Approach · Steps (owner, estimate, verified by) · Verification · Risks · Rollout and rollback · Open questions |
| Feature spec | `dev plan new "<title>" --kind spec` | Summary · Problem · Users and scenarios · Requirements (R1…, priority) · Out of scope · UX · API and data · Acceptance criteria · Metrics · Risks |
| Spike | `--kind spike` | Question · Timebox · Options · Method · Findings · Recommendation (with confidence) |
| Bug fix | `--kind bugfix` | Symptom · Reproduction · Expected vs. actual · Root cause (`file:line`) · Fix · Verification · Prevention |
| Decision (ADR) | `--kind adr` | Status · Deciders · Date · Context · Decision · Options considered · Consequences |
| Task | `dev task add "<title>" --type task\|feature\|bug\|spike --why "…" --done "…"` | By type: Context/Why/Symptom/Question · Scope · Steps to reproduce · Done when (testable checkboxes) |
| Steps of a run | `dev progress start "<goal>" --plan "step 1; step 2; step 3" --estimate 1h` | Checklist ✓ ▸ ○ with elapsed, estimate and ETA |
| Session activity | `dev handoff --log "<outcome; next>"` | `YYYY-MM-DD HH:MM · user@host · agent · branch · outcome · next` |
| Status block | the start of every final message | `` **[Firefly> DevKit]** <project> · `<branch>` · <task> ``, then **Done:** · **Check:** ✓/⚠/✗ evidence · **Progress:** (when a run is active) · **Next:** |
| Status report | `dev report [--days 7] [-i <initiative>]` | Summary table, then per initiative: health with reason · progress · milestone · done · in progress · next · blockers and risks · team |
| PR, merge, review, handover | `dev pr create`, `dev task delegate` | Sections 9–12 above |

Fill every placeholder (`_…_`) or delete the section if it does not apply. Never leave template text in a finished
record. Plans are versioned: after you change one, run `dev plan revise <file> --why "…"`.

## Time and predictability

- **Estimate before you start, then track it.** Re-plan and tell the developer at 1.5× the estimate. Don't silently
  overrun.
- **Work fast without cutting corners.** Run independent tool calls in parallel. Run long commands in the background.
  Run the narrowest check first and the full suite once at the end. Use targeted searches and compact output (see
  the `working-efficiently` skill). Never re-read what you already have.
- **Look at the history.** `dev time` shows how long turns, commands and subagents take here and how accurate past
  estimates were. Use it to plan realistically.
