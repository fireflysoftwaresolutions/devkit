---
name: work-state
description: Keeping devkit's records of work in flight true—work items and their locks, progress runs, steps and crew lanes, decisions and crew messages, waits and CI watches, the pinned install, and merged branches—for the tech lead and crew members on Claude Code and Codex. Use before you end a turn that started, changed or finished work, right after a merge, before a release, when `dev doctor`'s work state check or the dashboard's Health view reports stale work, when an item stays doing, `dev progress done` refuses to end a run, a decision blocks nothing, a CI watch expired, or merged branches pile up.
---

# Keeping work state true

devkit keeps a record of every piece of work in flight, and `dev status`, the dashboard, the session context, and the
next session all read it. A record moves only on an event devkit sees: a `dev` command, a hook, or a scheduled job.
When the event happens without one (a merge on GitHub, an agent stopped mid-step, a session that ends), the record
keeps its old state and every view tells a story that is no longer true. Move each record with its command when the
event happens, and repair what drifted when the checks show it.

## The records

| Record | States | What moves it |
| --- | --- | --- |
| Work item | todo, doing, blocked, done, cancelled | `dev task start\|done\|release\|block\|cancel`; a lock that runs out sends doing back to todo; `dev pr merge` closes the branch's items |
| Run | running, blocked, done, orphaned | `dev progress start\|done`; a session that ends leaves its run orphaned until `dev session carry` or a resume |
| Step | pending, running, blocked, done, skipped, unverified | `dev progress step\|reopen\|skip\|confirm`; a member's report |
| Crew lane | running, done, unverified, stopped, lost | the launch, the member's report, TaskStop, an interruption, silence |
| Decision | pending (or moot), decided, withdrawn, closed | `dev crew decide`, `dev crew withdraw`, the end of its run |
| Crew message | question (waits, or moot), notice and answer (never wait) | `dev crew ask`, `dev crew answer`, a `SendMessage` to the member who asked that names the question, the end of its run |
| Wait | live, expired, superseded, ended | `dev watch --wait`; its timeout, a newer wait, what it printed; `dev watches` lists them |
| CI watch | live, expired, ended | `dev ci watch --wait`; `dev ci watches` lists them |
| Install | the pinned release, or an older session | `dev install`, `dev update`, a restart |
| Branch | open, merged, pruned | the merge test; `dev pr merge`, `dev release`, `dev branch prune` |

**A work item lives on its branch.** `dev task start <id>` takes a lock for 8 hours (`work.lock_hours`) and links the
item to the work branch you are on; `dev branch new` and `dev wt new` link the one item you hold without a branch. The
lock renews only while one of your live sessions is on that branch. Work elsewhere and the lock runs out: the item goes
back to todo, and someone else may take it.

**Name an item by its id.** `<id>` is the item's id (`T-…`, `F-…`, `B-…`, `S-…`) or a unique prefix of it; ids never
move. The number in front of an item in `dev task` is for people at a terminal: it counts only in the list `dev task`
last showed that reader (your session, your crew lane, a Codex thread, or a terminal), for 30 minutes, and while the
open items are still that list. After a close, an add, or a move, devkit refuses the number and names both ids, and a
list it cannot tie to one caller has no numbers. Every write prints its item (`✓ done T-… “<title>”`): read it to
confirm you changed the item you meant.

**Now is for this week's work.** With more than five open items in a project's Now, `dev task add --now`,
`dev task move <id> --to now`, and `dev status` warn with the count. Move what waits to Next with
`dev task move <id> --to next --why "…"`.

**A closed item keeps the state it was closed in.** `dev task start`, `release`, `done`, `move`, `block`, and `cancel`
need an open item (todo, doing, or blocked) and refuse a done or cancelled one; `dev task unblock` takes only a blocked
one. A done item with work left comes back with `dev task reopen <id> --why "<what is left>"`, then moves like any open
item.

**A decision is open only while someone waits for it.** It is moot when the member that asked has ended and no step
is blocked on it: it needs no answer, never goes in the status block's **You** line, and does not stop
`dev progress done`. Ending or replacing the run closes every decision still pending in it, with the reason. A
question to the tech lead is moot the same way: its member ended and holds no open step, so it wakes nobody and
needs no answer.

**A wait is live only while its process runs.** `dev watch --wait` is `live` until it prints what arrived (`ended`),
times out or is stopped first (`expired`), or a newer wait of the same session and consumer takes over
(`superseded`). One live wait per consumer: the main session, or one crew lane. The `wait-and-wake` skill says when
to arm one.

**A lane holds a slot only while it works.** A member stopped with TaskStop, or whose transcript shows an interruption,
ends `stopped`: its slot frees and its open steps go back to pending. A resumed member takes a slot like a launch;
while none is free it may read but not write.

## Who runs what

| Who | Commands |
| --- | --- |
| The tech lead | `dev progress done\|confirm\|reopen\|skip` and `dev crew decide\|withdraw`, typed in the main session: run through a script or `bash -c`, they are refused (Codex runs them and records them as unverified). The repairs: `dev task reconcile --apply`, `dev branch prune --apply` (under the irreversible-action gate when it deletes on origin) |
| A crew member | `dev progress step\|note` and `dev crew ask\|answer`, in its own lane, and `dev watch --wait --for me --timeout 9` while blocked on an answer; it asks the tech lead for a repair. It never runs `dev branch prune --apply` (unless `--local` or `--offline`) or `dev sync --scheduled`: the guard counts both as deletions on origin |
| The developer | answers to decisions; `dev install`, `dev update`, and the restart of their sessions; the yes before `dev branch prune --apply` deletes branches on origin |

## Before you end a turn

1. Close each step you finished with its evidence: `dev progress step "…" --check "…"`. A step still running gets a
   `dev progress note`, never a close.
2. The item you worked on: `dev task done <id> --why "<evidence>"` once it is verified, or `dev task release <id>` if
   you stop. Never leave it doing on a branch no session is on.
3. While events can still arrive (a member works, a launch is pending, a step waits for a slot, CI you sent runs),
   a wait that wakes you is armed: `dev watch --wait`, which covers CI too (the `wait-and-wake` skill has the
   command for each agent).
4. The **You** line names only open decisions that wait for the developer. Moot and closed decisions and notices
   never go there, and a crew question to the tech lead is yours to answer (`dev crew answer`).
5. Ending the run: `dev progress done "<outcome>"` is refused while a member owns a running step. Wait for its report,
   or skip the step on purpose with `dev progress skip "<step>" --why "…"`.

## After a merge

- `dev pr merge` marks the open items of the merged branch done, with the PR and the merge commit in their changelog,
  and deletes the merged branch with its clean worktree (`--keep-branch` keeps it). Arm a watch on the merge commit:
  `dev ci watch --wait --pr <n>`.
- Merged somewhere else (GitHub's web page, another Mac)? Run `dev task reconcile` and `dev branch prune`, and apply
  what they report.
- An item the merge closed too early: `dev task reopen <id> --why "<what is left>"`.

## Before a release

1. `dev task reconcile`, then `dev task reconcile --apply`: the open items of branches merged since the previous tag
   are done, with the merge commit as evidence.
2. `dev release` refuses while such an item is open. When one truly has work left (reopened with what is left),
   `--why "…"` releases anyway and records the reason in the audit and the output; never use it to skip the check.
3. `dev branch prune`: no merged branch or worktree left behind. Once the release is out (not a `--draft`),
   `dev release` deletes the branches it merged.
4. After the tag, arm a watch on it: `dev ci watch --wait --tag v<version>`.

## Read the checks

- **`dev doctor`, row "work state"** (Workspace group), for the project you are in: either "no stale work items or
  runs", or one line per finding with its fix: doing items without a lock, open items whose branch is merged, items
  that share a title, and open runs with no step change for `work.lock_hours` or that began before the latest
  release tag. It changes nothing.
- **The dashboard's Health view, panel Work state:** the same rows for each recently active project. Its Now view
  lists only open decisions, waiting questions, and each branch's newest settled CI result; moot decisions sit on a
  quiet line under their run.
- `dev doctor` also says whether GitHub deletes a merged PR's branch (`delete_branch_on_merge`) and names a file an
  older devkit copy wrote.

## Repair drift

| What you see | Check | Fix |
| --- | --- | --- |
| An item doing with no lock, or nobody on its branch | `dev task reconcile` | `dev task reconcile --apply` sends it back to todo |
| An open item whose branch is merged | `dev task reconcile` | `--apply` marks it done with the merge commit; or `dev task reopen <id> --why "…"` if work is left |
| Two items with one title | `dev task reconcile` | `dev task cancel <id> --why "duplicate of <other id>"` |
| A run that stopped moving or began before the latest release | `dev progress` | in its session: `dev progress done "<outcome>"`, or `dev progress step`; after `/clear`: `dev session carry` |
| A step flagged for a recheck after a reopen | read its work against the change | `dev progress confirm <step> --why "…"` when it still holds; otherwise `dev progress reopen <step> --why "…"` |
| A member stopped or lost | `dev crew next` | relaunch it with `dev crew brief <member> "…" --step "<step>"`, or `dev progress skip "<step>" --why "…"` |
| A decision that no longer applies | `dev crew decisions` | the tech lead runs `dev crew withdraw <id> --why "…"` |
| A question nobody answered | `dev status` | `dev crew answer <id> "…"` |
| A watch expired while its run goes on | `dev ci watches` | run the re-arm command it prints |
| A wait expired while members still work | `dev watches` | run the arm-again command it prints |
| Merged branches and their worktrees | `dev branch prune` | `dev branch prune --apply`, after the developer agrees |
| A session older than the pinned install | the one-time notice, `dev doctor` | the developer restarts the session |

`/devkit:reconcile` runs these checks in one pass and applies the safe fixes once the developer agrees.

## Common mistakes

- **Closing a step or an item to send an update.** Use `dev progress note`; close only on evidence.
- **Withdrawing a decision to avoid asking the developer.** `dev crew withdraw` is for a decision that no longer
  applies: the plan changed, the step went away, or it was asked by mistake. It is never the developer's answer and
  opens no irreversible action. A decision that still matters waits for the developer.
- **Treating a moot decision as waiting.** It needs nobody; leave it, or withdraw it with its reason.
- **`--why` on `dev release` instead of reconciling.** The reason is recorded, and the items stay wrong.
- **Starting an item on `main`, then working on another branch.** The lock never renews there and runs out after 8
  hours. Start it on its branch, or create the branch with `dev branch new`.
- **Leaving merged branches behind.** They hide which work is still open; prune them.
- **Moving or cancelling a done item.** It is refused: reopen it with what is left, then change it.
- **Running devkit from an old release folder.** Use `dev` on the PATH; an older copy writes no managed config and
  only tells you to restart.
