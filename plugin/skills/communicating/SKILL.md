---
name: communicating
description: How to keep the developer informed during work — kickoff plans with estimates, progress updates during long runs and subagents, status answers ("how is it going?"), decision requests, clear didactic explanations, final reports and failure reports — with templates and the `dev progress` command that records progress, computes ETAs and publishes it to the team in real time. Use at the start of any non-trivial task, every few minutes of silent work, whenever you launch or receive background work, when you need a decision, and at the end.
---

# Communicating with the developer

The developer should never have to ask "how is it going?". Tell them before they wonder. Every message follows the
same rules:

- **Lead with the answer.** The first line is the outcome, the verdict or the question. Details come after.
- **Use their language.** Reply in the language the developer writes in (Spanish or English), with short sentences and
  no internal jargon. Define a term the first time you use it.
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
Left: <remaining steps> · Risks: <top risk> · From you: <decision or nothing>
```

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

It starts with the DevKit status line (global instructions §0). After that line:

```
<Outcome in one or two sentences.>
**Changed:** <files / commands / config> · **Evidence:** <tests: 87 passed; command output; PR #>
**Not done / risks:** <honest list, or "none"> · **Next:** <the next step, who does it>
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

## Time and predictability

- **Estimate before you start, then track it.** Re-plan and tell the developer at 1.5× the estimate. Don't silently
  overrun.
- **Work fast without cutting corners.** Run independent tool calls in parallel. Run long commands in the background.
  Run the narrowest check first and the full suite once at the end. Use targeted searches and compact output (see
  the `working-efficiently` skill). Never re-read what you already have.
- **Look at the history.** `dev time` shows how long turns, commands and subagents take here and how accurate past
  estimates were. Use it to plan realistically.
