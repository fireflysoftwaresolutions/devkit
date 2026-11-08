---
name: working-efficiently
description: How to spend tokens, models, reasoning effort and parallelism without losing quality—which work deserves the strongest model and high effort and which can go to a small, fast one; when a subagent or a parallel session pays off and when it is waste; briefing templates; context and tool-output hygiene; escalation rules and quality gates. Covers Claude Code and Codex. Use when you choose a model or effort level, before you launch a subagent or parallel session, when a session grows long or its output floods the context, and when a cheaper attempt fails.
---

# Working efficiently without losing quality

Quality is the constraint; tokens are the budget. The cheapest run is the one that is right the first time: a wrong
fix, a missed edge case or a red CI run costs more than any tokens you saved getting there. Save on context and on
mechanics, never on judgment.

## 1. Model and effort routing

**The main session runs on the model and effort the user chose.** Don't switch them mid-task on your own: each switch
re-reads the whole context uncached. If the work needs a different tier, say so in one line and let the user switch,
or suggest it for the next session.

| Work | Model | Effort | Why |
| --- | --- | --- | --- |
| Architecture, design trade-offs, ambiguous requirements | session model; suggest the strongest (Fable, Astra) for big calls | high–xhigh | A wrong direction multiplies every later cost |
| Debugging without a clear repro, flaky tests, concurrency, code you didn't write | session model | high, xhigh if stuck | Correctness depends on edge cases that are easy to miss |
| Security review, auth, money, personal data, migrations, public API changes | session model or stronger, never smaller | high | Misses are silent and expensive |
| Final review before "done" or a PR | fresh context, session model | high | Independence matters more than tier |
| Routine implementation with a clear scope, and its tests | session model | default (medium on Claude 5.5 models) | The default is tuned for this |
| Search, file and symbol lookups, "where is X used," config values | small read-only agent | low | The result is cheap to check |
| Log triage, summarizing long output | small agent | low–medium | Only if it returns quotes and line references you can check |
| Mechanical edits from a pattern you specified, formatting, lint | a tool (formatter, LSP rename, codemod, sed); else a small agent | low | Deterministic beats any model |

- Raise effort for one hard turn instead of the whole session: `ultrathink` in the prompt (Claude); pick the level
  in `/model` (Codex). `max` has diminishing returns and tends to overthink: use it only for hard unattended work.
- **A downgrade backfires when:** the answer is a judgment rather than a lookup; the output can't be checked cheaply
  (a summary you can't verify, "no issues found"); the brief leaves room for interpretation; the work is tightly
  coupled to what you are doing; or a cheaper attempt already failed. **Rule: downgrade only work whose result you can
  verify cheaply.**
- Prices per million tokens in/out are Fable 5.1 $10/$50 (usage credits on subscriptions), Opus 5.5 $4/$20, Sonnet 5.5
  $2/$10, and Haiku 5.5 $0.10/$0.50 for prompts up to 100k tokens ($0.50/$2.50 for longer prompts). On Codex, GPT-6.1 Sol is the
  workhorse, GPT-6 Astra the frontier model and GPT-6 Luna the fast, affordable one.

## 2. Subagents and parallel sessions

A subagent has its own context, starts cold, sends its own requests, and returns a summary. It buys parallelism, a
clean main context or an unbiased reviewer, never a smarter answer. Multi-agent runs use about 15x the tokens of a
chat (Anthropic). For Codex this section is the explicit instruction its spawn tool asks for: delegate in the
**Delegate** rows, otherwise work locally.

| Situation | Decision |
| --- | --- |
| Broad search across many files or naming conventions, where you need only the conclusion | **Delegate** to one read-only search agent |
| 2–4 independent questions (separate modules, code vs docs vs logs) | **Delegate** in parallel, all launched in one message |
| Verbose work: full test suite, long logs, documentation fetches | **Delegate** or run in the background, and get a summary back |
| Independent review of your own change | **Delegate** to a fresh reviewer (`/code-review`, Codex `/review`) |
| Implementation split into disjoint write sets that land independently | **Delegate** one worker per slice, each owning its files |
| Separate tasks lasting hours, or the user wants parallel streams | Parallel sessions in worktrees (`dev wt new <type>/<topic>`), not subagents |
| A lookup you can do in 1–3 searches or one LSP call | Do it yourself |
| Your next step is blocked on the result | Do it yourself |
| Edits that change the same files or depend on each other | Do it yourself, in sequence |
| A side task that needs what you already know | Claude: a fork (inherits context and cache). Codex: `fork_turns`. Otherwise do it yourself |
| "A second attempt might do better" | Don't delegate; escalate (section 5) |

- **How many:** one for a search, 2–4 for independent questions, rarely more than 5. Each one costs a full context.
- **Model and effort:** subagents inherit the session model by design in both tools. Don't set a model on the
  Agent/spawn call unless the user asked. For mechanical lookups use an agent whose definition pins a small model
  (the crew's read-only Sparks: `devkit:sparks` in Claude Code, agent type `sparks` in Codex; `scout` still works
  as its alias) and low effort. This skill asks for `effort: low` on
  mechanical, self-checking briefs and `effort: high` on reviews of risky changes (Claude Agent tool: `"lower"` or
  `"higher"` also work).
- **No duplicate work:** while an agent runs, do non-overlapping work; never redo its task. Give each agent a distinct
  question or write set. Send follow-ups to the same agent (Claude `SendMessage`, Codex `send_input`/`followup_task`)
  instead of starting a new one on the same question.
- **Wait without polling:** background agents notify you when they finish. In Codex, call `wait_agent` only when you
  are blocked, with a long timeout.
- Tell the developer what runs in the background and relay what it found (`communicating` skill, section 4).

## 3. Briefing templates

The brief is the agent's only context. If you can't write a clear one, you don't understand the task well enough to
hand it off. Give paths, not pasted file contents.

### Lookup or research

```text
Goal: <the decision this answer feeds, one sentence>
Questions: 1. … 2. …
Scope: <dirs, files, docs>. Out of scope: <…>
Known / ruled out: <facts, paths and hypotheses already checked>
Return: <format, e.g. path:line + one-line finding per item, under 300 words>. Mark each item verified or inferred.
Say "not found" rather than guess. Don't edit files or start agents.
```

### Worker (code change)

```text
Goal and acceptance: <behavior; the test that must pass>
Write scope: only <files/dirs>; don't touch <…>
Context: <interfaces, conventions, decisions already made and why>
Verify: run <narrow test command>; report the command and its result.
Return: files changed, what changed and why, test output summary, open questions.
```

### Reviewer

```text
Review <diff range or files> against <spec, plan or task>. Report only correctness, security and missed-edge-case
problems: file:line, severity, and a concrete failing scenario. Skip style. If nothing material, say so.
```

**Check what comes back.** A report is a claim, not evidence. Spot-check one or two cited paths or lines; read the
diff of delegated edits (`git diff --stat`, then the files that matter); run the tests yourself before you say they
pass. A cheap agent's "nothing found" in a risky area proves nothing: redo it at the session model.

## 4. Token hygiene

- **One task per context.** Start fresh between unrelated tasks (Claude `/clear`, Codex `/new`): it is free, and the
  next task stops paying for the last one's history. The devkit session context and handoff note carry what matters.
- **Compact at natural breaks** with a focus (`/compact keep the failing test and the plan`), not mid-task. To abandon
  a wrong path in Claude, `/rewind` instead: it returns to an already cached prefix.
- **Keep the prompt cache warm.** Choose model, effort and fast mode at the start of a session: a mid-session model
  switch or fast-mode toggle re-reads the whole context uncached, and so does a skill whose frontmatter names another
  model. Effort changes keep the cache on Claude 5.5 models and Fable 5.1 (API key or subscription).
- **Targeted reads.** Search first (LSP, Grep/`rg -n`, `git ls-files`), then read only the lines you need. Don't read
  whole directories or large files end to end, and don't re-read a file you just read or edited.
- **Compact output.** Ask for less instead of reading more: `git diff --stat` before the full diff, test runners'
  quiet or failures-only modes, `gh run view --log-failed`, `| tail -n 80`, `| grep -E 'FAIL|ERROR'`. Never cut away
  the error lines you need for the diagnosis.
- **Fewer, larger tool calls.** Batch independent reads, searches and commands into one message so they run in
  parallel.
- **No sleep or poll loops.** Commands that run for more than a minute or so while you have other work go to the
  background (Claude `run_in_background`, Codex background terminals, `/ps`); you are notified when they finish. Use
  watchers like `gh pr checks --watch`. Run in the foreground only when the next step needs the result right away.
- **Waits that must wake you.** An idle session wakes only for its own tasks. Claude Code: a Monitor (one event per
  output line, at most 30 minutes, re-arm it) or a background command (one notification when it exits, no limit).
  Codex: nothing in the background wakes it, so wait in the foreground with a timeout, or check at the next turn.
  For CI, the command is `dev ci watch --wait` on both (`cicd` skill, section 6).
- **Lean always-on context.** Prefer CLIs (`gh`) to MCP tools that do the same job, and keep workflow detail in skills,
  not in AGENTS.md/CLAUDE.md. `/usage` (both tools) and `/context` (Claude) show where tokens go and what is loaded.

## 5. Escalation and quality gates

**Escalation ladder.** Move up a step as soon as one of the triggers applies; don't retry at the same tier.

1. A small agent fails, hedges or returns something you can't verify → redo it on the session model.
2. Two failed attempts at the same problem → stop patching. Re-read the evidence, state a new hypothesis, raise
   effort (high or xhigh, or `ultrathink` for the turn).
3. Still stuck, or the stakes are high → tell the user and propose a stronger model (Claude `/model fable`; Codex
   `/model`, or `--profile deep` when installed). On long Claude tasks, the user can also add an advisor
   (`/advisor opus`).

**Escalate immediately, before any attempt:** ambiguous requirements (ask one focused question); security, auth,
money, data or migrations; irreversible or external actions; conflicting evidence; a fix that doesn't make the
failing test pass.

**Gates that hold whatever the tier:**

- Evidence before claims: run the check in this session before you say "done," "fixed," or "passes."
- The narrowest check after each change, the full local suite before done (`delivery` skill: what CI runs).
- Risky changes (auth, input handling, secrets, migrations, concurrency, public API) get an independent review in a
  fresh context at high effort: `/code-review`, Codex `/review`, `/security-review`; see the `devsecops` skill.
- A cheaper model's "looks fine" is never the only review of a risky change.

## 6. Tool notes

**Claude Code.** `/model` saves your pick as the default for new sessions; `--model` applies to one session. Effort:
`/effort`, `ultrathink`, `--effort`. The built-in Explore and Plan agents already run on a strong model (the session
model, Explore capped at Opus). Fast mode (`/fast`) is the same Opus at double the price, billed to usage credits on
subscriptions: only for latency-critical interactive work, switched on at the start of a session. The advisor
(`/advisor`, experimental) lets a cheaper main model consult a stronger one at decision points; it is the user's
choice.

**Codex.** `/model` sets model and reasoning effort (saved to config.toml); Plan mode keeps its own effort. Subagents
inherit the parent's model and effort; custom agents in `~/.codex/agents/*.toml` can pin a smaller model. Profiles
layer `~/.codex/<name>.config.toml` over the base config: `codex --profile deep|fast|review` when they are installed
(`ls ~/.codex/*.config.toml`). `/review` uses `review_model`, which defaults to the session model. Codex itself warns
that high reasoning effort can quickly consume Plus plan rate limits.

## 7. devkit compact commands

Prefer devkit's token-light commands to raw shell output. They are capped, respect `.gitignore` and fold noise:

| Instead of | Use | What you get |
| --- | --- | --- |
| `ls -R`, `tree` | `dev files [path]` | a capped tree with sizes, git status, and folded build output |
| `find` | `dev find <pattern> [-e ext] [--changed]` | gitignore-aware paths relative to the current folder |
| `grep -r` | `dev grep <regex> [-l] [-C n] [--ast]` | `file:line: text` with a cap per file and a total cap |
| `git diff` | `dev diff [--staged] [--names]` | the stat first, then `-U1` hunks; lock and generated files folded |
| `rm`, `mv`, `cp` | `dev rm`, `dev mv`, `dev cp` | to the Trash with undo; refuses unsaved work or open files and names the flag |

For CI, `dev ci status` gives the jobs, the current step and an ETA, in place of reading `gh run view` output.
