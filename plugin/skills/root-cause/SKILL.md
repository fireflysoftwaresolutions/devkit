---
name: root-cause
description: "Find and prove the root cause before fixing anything. Use for any error, failing test, CI failure, crash, regression, flaky result, wrong output, or incident, before you change code to fix it; when the same command failed twice; when a fix did not hold; when a guard refusal repeats; and before a `fix` or `revert` commit, which needs its root cause (`dev commit --cause`). Also says when to escalate, and when to bring in the Performance analyst (The Merovingian) or the Incident commander (Commander Lock)."
---

# Root cause first

A fix that does not name a proven cause is a guess, and guesses come back: the same refusal seven times, the same
lesson three times. This procedure finds the cause, proves it with an experiment, fixes that cause, and records it
so nobody debugs the same thing twice.

**No fix without a proven cause.** Changing code to see whether the error goes away is not debugging; it is how
patch loops start. The `superpowers:systematic-debugging` skill has the techniques in depth (tracing data backward,
instrumenting each layer); this skill is devkit's procedure and what it records. Where they differ, this one wins.

## The procedure

1. **Stop changing code.** Revert any speculative edit you made since the failure appeared (`git diff` shows them).
   Two unproven changes on top of each other hide which one mattered.
2. **Reproduce it.** Write down the exact command and the output line that shows the failure. Best is a failing
   test; otherwise a minimal reproduction in your scratch folder (never in the repository). A failure you cannot
   reproduce is not understood yet: collect more evidence (logs, the CI run with `dev run view <id> --failed`, the
   inputs) until you can.
3. **Look up what is known.** Before you form a theory:
   - `dev lesson --search "<area or error text>"` and the lessons your context or brief recalled;
   - `dev bug search <words or fingerprint>` when a `dev` command, a hook, or the guard failed;
   - the handoff note, and what changed recently (`git log --oneline -15 -- <file>`).
4. **Read the error and the code path.** Read the whole message and stack trace, then the code from the entry point
   to the failing line. List at least two hypotheses, each with the observation that would prove or rule it out.
5. **Prove one cause with an experiment that flips the result.** A failing test that passes when you change only
   the suspected cause; a minimal reproduction; `git bisect run <test command>` for a regression; one log line that
   shows the wrong value where you expected it. Reasoning alone proves nothing: if you cannot flip the result, you
   have a hypothesis, not a cause.
6. **Fix the cause, not the symptom.** Change the code where the cause lives, and say in one sentence why the fix
   removes it. A retry, a sleep, a broader `catch`, a null check, or a skipped test hides a symptom: use one only as
   a stated, tracked mitigation, never as the fix.
7. **Verify.** The reproduction passes; the affected tests pass (`dev test`); the new test fails without your fix
   (`dev test prove`).
8. **Record it.** The cause goes in the commit (`dev commit --cause`); a lesson when it can repeat; a bug report or
   detour when the defect is devkit's (below).

## The 5-whys check

Ask "why?" of each answer until the answer is something you can change and test. A cause that is itself a symptom
is not the root.

| Question | Answer | Root? |
| --- | --- | --- |
| Why did customers see the old price? | The page served a cached response | No: a symptom |
| Why was the cached response stale? | The update did not miss the cache | No: still a symptom |
| Why did it not miss the cache? | The cache key is the product id only; the price version is not in it | **Yes:** a defect you can change and test |
| Why did no test catch it? | No test reads a price after an update | The prevention: a regression test and a lesson |

Stop when the answer is a defect, a missing check, or a rule you can change. Going further ends in "people make
mistakes," which nobody can fix.

## The template

Fill it in your report, the PR, or a bugfix plan (`dev plan new "<title>" --kind bugfix` has the same sections,
for a bug that needs a written record):

```markdown
**Root cause · <the failure, in a few words>**
- Symptom: <what fails, where, since when>
- Reproduction: `<exact command>` → `<the output line that shows it>`
- Known: lessons <ids, or none found>; devkit bugs <fingerprints, or none>; recent changes <commits, or none>
- Hypotheses: 1. <…> (ruled out: <evidence>) · 2. <…> (proven: below)
- Proof: <the experiment that flipped the result, and its output>
- Cause: <one sentence, with `file:line`>
- Fix: <the change>; it removes the cause because <…>
- Verification: <commands and their results>
- Recorded: commit `Root cause:`; lesson <id> or none; bug report <fingerprint> or none
```

## Record it

```bash
dev commit -m "fix(prices): include the price version in the cache key" \
  --why "Customers saw the old price for up to an hour after an update." \
  --cause "the cache key was the product id only, so an update never missed the cache" \
  --verified "dev test: 14 passed; dev test prove: the new test fails without the fix"
```

- **Every `fix` and `revert` commit carries `--cause`.** `dev commit` writes it as a `Root cause:` paragraph after
  the why, and refuses a fix or revert without it, or with a placeholder (`unknown`, `n/a`, `tbd`, `-`, fewer than
  15 characters).
- **A lesson when it can repeat** (the `learning-loop` skill):
  `dev lesson add "<the rule>" --kind bug --cause "…" --prevent "…"`, with an anchor (`--file`, `--command`, or
  `--error`). A known lesson that happened again gets `dev lesson recur <id> --evidence "…"` instead.
- **devkit's own defect** goes to the bug registry, not to a lesson: apply the detour devkit gives; when a way you
  found works, record it with `dev bug detour <fingerprint>`; after two failed attempts, run
  `dev bug report <fingerprint>` and ask the developer (the `learning-loop` skill, "When devkit itself fails").

## Escalate after two failed attempts

Two fixes that did not hold on the same problem mean the cause is not proven. Stop, and escalate with what you
ruled out and the evidence:

- **A crew member** asks the tech lead: `dev crew ask "<the failure, what was ruled out, what you need>"`, and goes
  on with work that does not depend on it.
- **The tech lead** raises the effort or the model, sends it to the Senior engineer (Neo), who is Responsible for
  `debug` work, or asks the developer.
- **When devkit says "The same failure twice,"** it saw one command fail twice with the same error. Start again at
  step 1 with the lessons and bugs it lists; do not make a third attempt of the same kind.

## Bring in a specialist

| Sign | Who | What changes |
| --- | --- | --- |
| Slow responses, timeouts, memory or CPU growth, a cost spike, a failure that appears only under load | Performance analyst (The Merovingian), `performance` work | The proof is a measurement: percentiles before and after, a profile, a trace |
| Users are affected now, data is at risk, a service is down, or a breach is suspected | Incident commander (Commander Lock), `incident` work | Stabilize first (roll back, scale, switch off), then find the cause; the postmortem records it |
| A `dev` command crashed, a hook failed, or the guard refused something it should allow | Nobody on the crew: the bug registry | `dev bug search`, the detour, `dev bug report` |

A crew member does not launch them: it asks the tech lead, who assigns the step. In an incident, a rollback is a
mitigation, not the fix: the root cause still gets found, fixed, and recorded.

## Rationalizations

| Thought | Reality |
| --- | --- |
| "The cause is obvious, I'll fix it now" | Obvious causes are the ones nobody proved. The experiment takes minutes. |
| "One more try with a small change" | That is the third unproven guess. Stop at two and escalate. |
| "It passes now" (after a rerun) | A pass you cannot explain is flaky, not fixed (the `testing` skill, flaky tests). |
| "Add a retry, a null check, or a broader catch" | That hides the symptom. Ask why the value is null. |
| "CI is flaky" | Only with evidence: `dev run rerun --flaky` needs it. |
| "I'll write the cause later" | `dev commit` refuses a fix without `--cause`. Write it now, while you know it. |

## Red flags: go back to step 2

- You are about to change code and cannot state the cause in one sentence.
- The same command failed twice with the same error.
- You changed two things at once, or you do not know which change fixed it.
- The fix adds a retry, a sleep, a skip, or a wider exception handler.
- You are thinking "this should work now."

## Done when

- The cause is one sentence with `file:line`, and an experiment proved it.
- The reproduction failed before the fix and passes after; `dev test` and `dev test prove` pass.
- The commit carries `Root cause:`; a lesson, a recurrence, or a bug report exists when the mistake can repeat.
