---
name: learning-loop
description: Learn from mistakes so they do not repeat or pile up across projects. Record a blameless lesson (what happened, root cause, fix, prevention, detection) after fixing a bug, a revert, a CI failure, a guard refusal you hit twice, or a correction from the developer; recall the known pitfalls at the start of work; count repeats; and turn recurring lessons into automation (a test, lint rule, guard rule, required skill or CI check). Uses `dev lesson add|ls|recur|prevent|retire|scan|review`.
---

# Learning loop

A mistake is cheap the first time and expensive every time after. devkit keeps lessons in the handoff repository
(`lessons/<project>/`, `lessons/initiatives/<slug>/`, `lessons/global/`). Sessions see the relevant ones as "Known
pitfalls" in their context. Recurring lessons are turned into automation, so nobody has to remember them.

## When to record a lesson

Record one when any of these happens and could happen again:

- **You fixed a bug.** The Stop hook reminds you after a `fix:` or revert commit.
- **A change was reverted, or a task went back to "doing" more than once.**
- **CI failed for a reason the local run did not catch.**
- **The guard refused the same kind of command twice.** `dev lesson scan` lists these.
- **The developer corrected you,** for example "that is not how we…" or "you forgot…".

Skip trivial typos and one-off environment hiccups.

## How to write it (blameless, specific, short)

```bash
dev lesson add "Releases tagged before the CHANGELOG section existed" \
  --kind process --severity medium \
  --happened "v26.11.2 shipped with an empty release note" \
  --cause "the release checklist lived in a wiki page nobody opened" \
  --fix "wrote the notes and edited the GitHub release" \
  --prevent "release only with dev release, which refuses a tag without a CHANGELOG section" \
  --detect "dev release --dry-run shows the notes before tagging" \
  --evidence "PR #212" --evidence "release v26.11.2"
```

- **Root cause, not symptom.** Ask "why" until the answer is something you can change: a missing test, an unclear
  rule, a tool that allows the wrong thing.
- **The prevention is a rule someone can follow tomorrow,** stated as an action.
- **Choose the scope.** `-p` (default: the project you are in), `-i <initiative>` for a programme, `--global` for
  mistakes any project can make.
- **Never put secrets or personal data in a lesson.**

## Recall before you work

- Read the "Known pitfalls" in your session context and follow their prevention.
- **Search before you touch an area:** `dev lesson ls --search "<area>"`.
- **If a known pitfall happens again:** `dev lesson recur <id> --evidence "…"`.

## Turn lessons into automation

A lesson that is only text will be forgotten. Prefer, in this order:

1. **A test** that fails on the mistake: a regression test for a bug.
2. **A lint, type or schema check.**
3. **A guard rule** that refuses the command (`lib/devkit/guard.py`), for workflow mistakes.
4. **A required-skill rule** (`config/skill-rules.json`) when the mistake was skipping expertise.
5. **A CI check** in the project's pipeline.
6. **A written rule,** in the project's AGENTS.md or the devkit global rules, as a last resort.

Then record the prevention: `dev lesson prevent <id> --by test --ref "tests/test_x.py::test_y" --why "…"`.

Changes to devkit itself (a guard rule, skill rule or global rule) go through a devkit PR. Lessons in
`lessons/global/` are the backlog for those PRs.

## Review

`dev lesson review` lists lessons that recurred and are not prevented, which you should automate now, and lessons
not seen for four months, which you should retire with a reason or confirm. Run it before a release and in the
weekly status (`dev report`).
