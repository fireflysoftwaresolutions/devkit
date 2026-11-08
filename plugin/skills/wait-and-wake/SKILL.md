---
name: wait-and-wake
description: Waiting for what comes back without polling—`dev watch --wait`, the one wait for every devkit event (crew questions and answers, decisions for the developer, steps ready to launch, members that stopped or were lost, CI results), armed the right way for each agent (the Claude Code main session in the background, a crew member or Codex in the foreground), with its exit codes, re-arming after each wake, `dev watches`, and the Stop rule that sends back a turn with nothing armed. Use before you end a turn while crew members work, a launch is pending, a step waits for a crew slot or CI runs; when you are a crew member blocked on an answer; when the Stop hook sends a turn back for a wait; and when a wait ends.
---

# Waiting and waking

devkit tells you what to act on through hooks, and a hook runs only when your session does something. An idle
session hears nothing: a crew member's question, a decision for the developer, a step that became ready, or a CI
result waits until the developer happens to write. `dev watch --wait` is the fix. It blocks until something you
must act on arrives for your own session, prints each event once, one line each, and exits. Its exit is what wakes
you. Waiting costs no tokens; each wake costs one turn.

The hooks and the wait take from the same records, so an event reaches you once, whichever reads it first.

## 1. When to arm a wait

Arm one before you end a turn while events can still arrive:

- a crew member still works;
- a launch is pending (its agent has not started yet);
- a step waits for a crew slot;
- a CI run this session sent has not finished (devkit's background watch of it still runs).

When none of these holds, nothing can arrive: end the turn without a wait. `dev watch --wait` says so at once
(exit 2), and `dev watch` shows what can still arrive and whether a wait is armed.

A crew member arms one only when it is blocked on the answer to its own question.

## 2. How each agent waits

| Agent | Before it goes idle or while blocked | When woken |
| --- | --- | --- |
| Claude Code main session | `dev watch --wait` with Bash `run_in_background: true`; name its task id in your reply | Read what it printed, act on each line, then arm it again if events can still arrive |
| Claude Code crew member | After `dev crew ask`, keep working on what does not need the answer. When blocked: `dev watch --wait --for me --timeout 9` in the foreground, as a command of its own, with the Bash `timeout` at 600000 | Continue with the answer. With none, wait again only while still blocked, or end with the question under "Needs from the tech lead" |
| Codex main session | `dev watch --wait --timeout 10` in the foreground, with a command timeout longer than 10 minutes, repeated while members work; or end the turn saying what still runs and that you check it at the developer's next message | Act, then wait again |
| Codex crew member | As the Claude Code member, in the foreground | The same as the Claude Code member |

- **Claude Code main session.** A background command sends one notification when it exits and has no time limit, so
  the wait runs up to 240 minutes by default. Keep the Monitor tool for following one CI run job by job
  (`dev ci watch --wait --timeout 28`, see the `delivery` skill); a `dev watch --wait` already wakes you for CI
  results.
- **Crew member.** Run the wait as a command of its own, not joined to another with `&&` or a pipe: the hook then
  ties it to your lane, so it takes your messages and never the tech lead's. A foreground command in Claude Code
  stops at 5 minutes unless you raise its timeout, and at 10 at most, so the member's wait ends at 9.
- **Codex** has no background wake-up. It waits in the foreground, again and again, or says plainly when it looks
  again.

One wait covers everything, CI included: after a push, a merge, or a release from the main session, devkit's
background watch feeds the CI result into the same wait. Arming a second wait in the same session supersedes the
first.

## 3. What it prints

One line per event, devkit's own words first; a member's or CI's text is one quoted span with control and escape
sequences removed:

```text
devkit · question M7 from Backend engineer (Tank): “May I rename the column?” · answer: dev crew answer M7 "…"
devkit · decision D3 from QA engineer (Mouse) waits for the developer: “Export voided invoices too?” · ask it: dev crew decisions --ask
devkit · ready: review → dev crew brief seraph 'Review the API' --step review
devkit · member: QA engineer (Mouse) was lost (silent 20 minutes); its step tests waits for a new launch
watch: 4 events · arm again: dev watch --wait
```

Answer a question with `dev crew answer <id> "…"`, ask a decision with `dev crew decisions --ask`, launch a ready
step with the line it gives, and relaunch a lost member's step. A member's normal finish on Claude Code, a crew
limit change, and a failed handoff push are notices: they ride along with the next event and never wake you alone.

## 4. Exit codes

| Exit | Meaning | Do |
| --- | --- | --- |
| 0 | Events printed | Act on each line, then arm again if events can still arrive |
| 2 | Nothing can arrive, said at once | End the turn without a wait |
| 3 | The timeout, or the wait was stopped | Arm again with the command it printed, if you still wait |
| 5 | A newer wait of the same session and consumer took over | Nothing: the newer one waits |
| 1 | No devkit session here | Run it from the agent session that waits, not from a plain shell |

## 5. What reaches you without a wait

While you work, the hooks deliver the same events at your next tool call or prompt, and a prompt that wakes the
session for a background event (a task notification, an agent message) carries them too. Nothing reaches an idle
session without a wait. The Stop hook sends back once a main-session turn that ends while events can still arrive
and nothing is armed, naming the command for your agent.

## 6. Questions and answers

- Answer with `dev crew answer <id> "…"`: it closes the question in devkit's record and reaches the member at its
  next tool call or its wait.
- In Claude Code, a `SendMessage` from the main session to a member answers that member's open questions to you too:
  devkit records each as answered via SendMessage, with the text. Codex's `send_input` does not, so answer there with
  `dev crew answer`, then resume the member.
- A question whose member ended and holds no open step is **moot**: nobody waits for it, it wakes nobody, and
  `dev watch` shows it as moot with the reason. It needs no answer.

## 7. Never

- Sleep loops, repeated `dev watch` peeks, or `dev status` calls to see whether something arrived: the model pays
  for every look, and the wait does it for free.
- Ad-hoc wait scripts that read devkit's state files: they miss the told-once records, so events arrive twice or
  not at all.
- A crew member's wait in the background: a member never idles, it runs or it ends, so nothing wakes it when the
  wait exits.

## 8. Check and troubleshoot

```bash
dev watch                # a peek that marks nothing: what waits for you, what can still arrive, armed or not
dev watches              # every wait: live, expired, or superseded, with the command to arm each again; CI watches too
```

| Symptom | Cause | Fix |
| --- | --- | --- |
| The Stop hook sent the turn back for a wait | Members work or CI runs, and nothing is armed | Arm the wait for your agent (section 2), name its task id |
| `expired` in `dev watches` | It timed out or was stopped before anything arrived | Run the arm-again command it shows |
| `superseded` | A newer wait of the same session took over | Nothing |
| Exit 1 | Run from a plain shell | Run it in the agent session |

See also the `crew` skill (leading members while they run), the `communicating` skill (what to tell the developer
while you wait), and the `delivery` skill (CI after a push).
