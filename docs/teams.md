# Teams and machines

This guide explains how a team of developers, their Macs, and their agents share one handoff repository without
stepping on each other: work items with locks, a reason for every change of scope, versioned plans, append-only
session logs, presence, and merge rules that keep git from conflicting.

It applies equally to one developer with a laptop and a desktop, and to a team that shares a handoff repository.

## The rules in one minute

- **Claim before you work.** `dev task start <id>` takes a lock on the item and pushes it at once. Other machines see
  it, and their agents are told not to work on it.
- **Every change has a reason.** Changes of scope (`edit`, `move`, `block`, `cancel`, `link`) need `--why "…"`. Each change bumps the
  item's version, adds a changelog line (who, machine, agent, when, why), and makes one commit.
- **Write through `dev`.** Work items, session logs, and presence are written only by `dev`; the guard blocks direct
  edits, so versions, locks, and history stay correct.
- **Finish or give back.** `dev task done <id>` when the work is verified, `dev task release <id>` when you stop
  without finishing. A lock is renewed only while a session works on the item's branch; otherwise it runs out after
  8 hours and the item goes back to todo. `dev pr merge` closes the items of the branch it merges.
- **Look before you start.** `dev team` shows who is working on what, on every machine.
- **Name an item by its id.** `<id>` on this page is the item's id, such as `T-fy3k2m9q1ab`, or a unique prefix of
  it. At a terminal, the number `dev task` shows in front of an item works too, while that list is unchanged and for
  30 minutes ([Work states › Numbers and ids](work-states.md#numbers-and-ids)).
- **Changes travel in seconds.** Every commit, push, PR, release, and handoff change queues a sync that reaches
  GitHub within about 15 seconds; other machines get it at their next pull (see
  [Near-real-time sync](#near-real-time-sync)).

## Set up a shared handoff repository

Each developer gets a private `<login>/handoff` by default. To share one, point everyone at the same repository
before they install:

1. Create the repository once (for example `acme/handoff`, private) and give the team write access.
2. On each Mac, set `handoff_repo` in `~/.config/devkit/config.json` (or answer the `dev setup` question):

   ```json
   { "handoff_repo": "acme/handoff" }
   ```

3. Run `dev install --only handoff`. It clones the repository into `~/Development/.handoff` and writes the merge
   rules (see [Merge rules](#merge-rules)).

The same steps connect your second Mac to your own handoff repository.

## Work items

Each task, feature, bug, or spike is one Markdown file, `items/<scope>/<ID>.md`, where the scope is a project id
(`acme/billing`) or `initiatives/<slug>`. One file per item means two machines never edit the same file unless they
change the same item.

```markdown
---
id: F-fwmj1ge0dlf
type: feature
title: Define the refund event schema
scope: initiatives/payments
status: doing
bucket: now
priority: normal
assignee: maria
claimed_by: maria@laptop (claude)
claimed_at: 2026-11-05T17:13:59Z
lock_expires: 2026-11-06T01:13:59Z
due: 2026-11-20
links: []
version: 3
created: 2026-11-05T17:13:49Z
created_by: maria@laptop
updated: 2026-11-05T17:13:59Z
updated_by: maria@laptop
---
# Define the refund event schema

## Why
Both services must agree on the event before either ships.

## Done when
- [ ] The schema is merged in both repositories.

## Changelog
- v3 · 2026-11-05T17:13:59Z · maria@laptop (claude) · claim: lock until 2026-11-06T01:13:59Z
- v2 · 2026-11-05T17:13:55Z · maria@laptop (claude) · move: bucket=now — why: billing needs it first
- v1 · 2026-11-05T17:13:49Z · maria@laptop (claude) · create: feature “Define the refund event schema” in next — why: both services must agree on the event
```

| Field | Meaning |
| --- | --- |
| `id` | `T-` task, `F-` feature, `B-` bug, `S-` spike, then a time-ordered code that is unique across machines |
| `status` | `todo`, `doing`, `blocked`, `done`, or `cancelled` |
| `bucket` | `now`, `next`, or `later`: where it sits on the board |
| `assignee` | Who is expected to do it: the claimer by default, or the teammate from `dev task delegate` |
| `claimed_by`, `claimed_at`, `lock_expires` | The lock: who holds it (`user@host` and the agent) and when it ends |
| `links` | Links such as `initiative:<slug>`, which count the item toward an initiative's progress |
| `version`, `created*`, `updated*` | The audit trail; the changelog has one line per version |

The actor is your GitHub login, the machine's short host name, and the agent: `claude` or `codex` when an agent runs
the command (detected from its environment), `human` otherwise.

The item's changelog is the last `## Changelog` heading outside code blocks, at the end of the file, with only its
entries (one list item per version) after it. Plans use the same rule. A description or a plan can contain that
heading too, in a code example or as a section of its own, and devkit keeps those lines in the body. Frontmatter lines
that devkit doesn't know, such as another tool's keys, stay after devkit's own fields. Only `links` is read as a list,
so a title such as `[API] Export invoices [v2]` stays as you wrote it.

Every write compares the new text with the file on disk and refuses to drop a line that the command does not change,
so a file devkit misreads stops the command instead of shrinking (see
[Troubleshooting](troubleshooting.md#devkit-refused-to-write-a-task-or-plan-file)). In the frontmatter of a devkit
file (one with an `id:` line), a command may change a key's value but not remove the key, and every other line must
stay. A file without one is compared whole. devkit writes the new text to a temporary file in the same folder and
then swaps it in, so a reader never sees half a file.

### Lifecycle

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/diagrams/work-item-lifecycle-dark.svg">
  <img src="images/diagrams/work-item-lifecycle-light.svg" alt="Life of a work item: dev task add creates it as todo; start or claim takes an 8-hour lock and makes it doing; release, delegate, or a lock that runs out returns it to todo; done, or a merge of its branch, releases the lock and closes it; block marks an item blocked and unblock returns it to todo; cancel with a reason closes any open item." width="100%">
</picture>

| Command | From | To | Lock |
| --- | --- | --- | --- |
| `dev task add "…"` | — | todo, in Next (or `--now`, `--later`) | none (`--claim` takes it at once) |
| `dev task start <id>` · `claim <id>` | todo | doing, in Now | taken for `work.lock_hours` (8), pushed at once |
| `dev task release <id>` | doing | todo | given back, pushed at once |
| `dev task done <id>` | any open state | done | given back, pushed at once |
| `dev task block <id> --why "…"` | todo or doing | blocked | unchanged |
| `dev task unblock <id>` | blocked | doing while it is claimed, todo otherwise | unchanged |
| `dev task cancel <id> --why "…"` | any open state | cancelled | cleared |
| `dev task delegate <id> --to <login>` | any open state | todo when you held the lock | yours is given back; the teammate claims it |
| `dev task link <id> --to <slug> --why "…"` | any state | unchanged; it now counts toward the initiative | unchanged |
| Lock not renewed for 8 hours | doing (blocked stays blocked) | todo | cleared by the hourly sync on any machine |
| `dev pr merge` merges the item's branch | any open state | done; the changelog names the PR and its merge commit | cleared |
| `dev task reconcile --apply` | doing without a lock · open on a merged branch | todo · done | cleared |

Claiming a blocked item keeps it blocked; run `dev task unblock <id>` when the blocker is gone.

A closed item keeps the state it was closed in. `dev task move`, `block`, and `cancel` refuse a done or cancelled
item and name `dev task reopen <id> --why "<what is left>"`, and `dev task unblock` refuses any item that is not
blocked.

### Locks across machines

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/diagrams/handoff-sync-dark.svg">
  <img src="images/diagrams/handoff-sync-light.svg" alt="Two machines, one handoff repository: Maria's agent runs dev task start; devkit pulls, writes the lock into the item, and pushes it at once; Jordan's machine pulls at session start and on every sync, so Jordan's agent is told the task is claimed and its own claim is refused; while Maria works the Stop hook renews the lock and publishes presence and the session line; dev task done releases the lock and pushes at once; a lock nobody renews for 8 hours is released by the hourly sync on any machine." width="100%">
</picture>

1. **Claim.** `dev task start` pulls the handoff repository, checks the lock, writes it, commits, and pushes before it
   returns, and it reports success only when the lock is on the remote. If another machine pushed a change to the
   item first, devkit reads the item again as the remote has it and claims it again, so a lock taken there first
   refuses yours with the name of the holder. When the remote cannot be reached, the claim fails with "the lock did
   not reach the remote": the lock is committed on your Mac only, other machines do not see it, and the next
   `dev sync` pushes it. Run `dev task start <id>` again to confirm it.
2. **Respect.** Each session's context lists your claimed tasks and the tasks claimed by others with the line
   `Claimed by others — do NOT work on these`. It also names the people working on the same project right now.
   Changing an item that someone else holds is refused until the lock expires.
3. **Renew.** While a session works, the Stop hook's background sync (at most every 2 minutes) extends a lock your
   machine holds once less than half of the lock time is left, but only when one of your live sessions is on the
   item's branch: `dev task start` on a work branch links it (`branch:<name>`). When you claimed the item on the
   default branch, or before its branch existed, `dev branch new` and `dev wt new` link it to the branch they create
   and say so, if it is the only item you hold with no branch; with more than one, they name them, and `dev task start <id>`
   on the branch links the one it is for. An item with no branch, or whose branch nobody works on, is not renewed.
4. **Expire.** The hourly `sync` job on any machine releases locks whose time has run out, in one commit. A doing item
   goes back to todo, a blocked one stays blocked, and the changelog says whose lock expired and when.
5. **Finish.** `done` and `release` give the lock back and push at once. `dev pr merge` does it for every open item
   linked to the branch it merges.

The lock length is `work.lock_hours` in [Configuration](configuration.md).

### Shipped and stale items

The board shows only work that is really open, because three commands close or reset what has shipped or stalled:

| Command | What it does with work items |
| --- | --- |
| `dev pr merge <n>` | After the merge, marks every open item linked to the merged branch as done, clears its lock, and writes `merged in PR #<n> (<short sha>)` in its changelog. The output names the items it closed. `--auto` closes nothing until GitHub merges |
| `dev release` · `dev release --dry-run` | Lists the open items linked to the branches merged since the previous tag and refuses to tag while any is open. `--why "<reason>"` releases anyway; the reason goes in the audit (`dev.release.open-items`) and the output |
| `dev task reconcile` | Reports the project's doing items without a lock, its open items linked to a branch already merged into the default branch on `origin`, and items that share a title. It changes nothing |
| `dev task reconcile --apply` | Closes the items of merged branches (done, the merge commit as evidence) and sends the doing items without a lock back to todo, each with a changelog line, in one commit. Items that share a title are only reported |

A branch counts as merged when one of these shows it reached the default branch (for `dev release`, since the
previous tag):

- a merge commit names it, such as GitHub's `Merge pull request #7 from owner/feat/x` or git's `Merge branch 'feat/x'`;
- for a squash or rebase merge, which leaves no such commit, a merged pull request of the branch whose commit is in the
  default branch;
- for a fast-forward, the branch's last commit is in the default branch.

Branch names come back, such as a second `fix/login-timeout`. A merge counts for an item only when it is newer than the
moment the item was linked to the branch, so the merge of the name's earlier use never closes the new item.

To recover:

- **An item went back to todo while you were still on it.** Your session was on another branch, or on the default
  branch. Run `dev task start <id>` on the work branch: it takes the lock again and links the branch, so the lock is
  renewed from then on.
- **An item was closed by a merge but has work left.** Run `dev task reopen <id> --why "<what is left>"`.
- **`dev release` refuses because of open items.** Close the shipped ones (`dev task done <id>`, or
  `dev task reconcile --apply`), or release anyway with `dev release --why "<reason>"`.
- **Two items share a title.** Keep one and cancel the other: `dev task cancel <id> --why "duplicate of <other id>"`.

`dev doctor` and the dashboard's Health view report the same stale items, each with its fix.
[Work states](work-states.md) explains how work items, runs, decisions, and the other records drift, and what moves
each back.

### Reasons and history

`edit`, `move`, `block`, `cancel`, and `link` refuse to run without `--why`. Every other verb accepts it too. The reason goes into the
changelog line and the commit message, so `git log` in the handoff repository reads like a history of decisions:

```text
27be6fb F-fwmj1ge0dlf: move bucket=next (maria@laptop (claude)) — waiting for the API review
10d13c6 F-fwmj1ge0dlf: claim by maria@laptop (claude)
676d924 F-fwmj1ge0dlf: create feature “Define the refund event schema” (maria@laptop (human)) — both services must agree
```

`dev task show <id>` prints the fields, the description, and the history; `dev task history <id>` prints only the
history.

### Hand a task to a teammate

```bash
dev task delegate T-fy3k2m9q1ab --to jordan --why "Jordan owns the event bus" --due 2026-11-20
```

devkit releases your lock if you held it, sets `assignee` and the due date, records the reason, and prints a short
message that it also copies to the clipboard (`--no-copy` skips that). Paste it into Slack, Teams, or email:

```text
Hi @jordan, could you take this task: "Define the refund event schema"?

Why it matters: Jordan owns the event bus
Done means: the change is merged with tests, and the task is marked done.
Deadline: 2026-11-20
Task F-fwmj1ge0dlf in initiative payments

To pick it up, run `dev task start F-fwmj1ge0dlf -i payments`. It locks the task so nobody else starts it,
and `dev task show F-fwmj1ge0dlf -i payments` shows its full history. Ask me anything here.
```

Read it before you send it; it should sound like you. The `communicating` skill has the template for other messages
to teammates.

## Versioned plans

Plans and specs live in `plans/<scope>/` with frontmatter (id `P-…`, title, kind, scope, status, version, a hash of
the body, who created and last changed it) and a changelog at the end.

```bash
dev plan new "Refund events" --kind spec -i payments    # draft, version 1, from the spec template
$EDITOR ~/Development/.handoff/plans/initiatives/payments/2026-11-05-refund-events.md
dev plan revise ~/Development/.handoff/plans/initiatives/payments/2026-11-05-refund-events.md --why "added the retry rule"
dev plan approve <file>                                 # status approved, new version
dev plan supersede <file> --why "replaced by the v2 spec"
dev plan ls                                             # flags plans edited without a revision
```

An approval covers the text that was approved. When an approved plan's text changes, `dev plan revise` puts it back to
draft and its changelog line names the version the approval covered, such as `revised; back to draft: the approval
covered v3`; run `dev plan approve` again when the new text is agreed. A revision that leaves the text as it was keeps
the approval.

devkit compares the body with the hash it recorded. When the body changed without `dev plan revise`, `dev plan ls`
shows `✎ needs dev plan revise`, and the Stop hook does not let the agent finish its turn until it records the reason
for each plan it changed in that session. A plan written by another tool (Superpowers writes to the same folder) is
adopted into the format on its first revision: devkit adds its frontmatter and keeps every line of the file. When the
file starts with `---`, for another tool's frontmatter or a horizontal rule, those lines stay at the top of the body.
The kinds and their sections are in
[Templates](templates.md#plans-and-specs).

## Session logs

Every session that changes something adds one line to `sessions/<scope>/YYYY-MM.md`:

```text
- 2026-11-05 18:40 · maria@laptop · claude · `feat/refund-events` · 3 commit(s): … · status: … <!-- s:5f2c9a1b7d3e -->
- 2026-11-05 19:02 · maria@laptop · human · reviewed the schema with Jordan; next: publish from billing
```

The hooks write and refresh the first kind; `dev handoff --log "…"` adds the second. The files are append-only and
there is one per scope and month, so concurrent machines never conflict.

## Presence and `dev team`

Each machine writes only its own file, `presence/<user>@<host>.json`, on every sync: its tracked sessions (agent,
project, branch, start time, latest status, progress of the current run, running subagents, turn start, and whether
the session is still active) and the locks it holds. A session that has ended is removed before the sync that ends it
publishes presence, and a session marked not active is left out wherever presence is read.

```bash
dev team               # machines active in the last 24 hours
dev team --hours 4
```

The session context, `dev progress`, the dashboard's Team view and `dev report` read the same files.

## Near-real-time sync

A machine that waits for the hourly sync shows its teammates an old picture. devkit therefore syncs the handoff
repository right after anything happens that others should see:

| Event | Who notices |
| --- | --- |
| An agent runs `git commit`, `push`, `merge`, `tag`, `rebase`, `cherry-pick`, or `revert`, or `gh pr create`, `gh pr merge`, or `gh release create` | The PostToolUse hook |
| A `dev` command succeeds: `commit`, `push`, `pr`, `release`, `branch`, `rebase`, `wt`, `adopt`, `clone`, `new`, `delivery`, `initiative`, or `note` | `dev` itself |
| A task, plan, lesson, or delivery policy changes | The handoff write |
| A session starts | The SessionStart hook, which also pulls the claims of other machines |
| A turn ends | The Stop hook, at most every 2 minutes: the session line, lock renewal, and presence |

Each event queues one background sync, `dev sync --offline`, after a short delay (about 15 seconds; 10 after a
handoff write, one at session start). Events inside that window join the queued run instead of starting another, so
a burst of commits makes one sync, which takes about 3 seconds. The output goes to
`~/.local/state/devkit/logs/sync.log`.

When the push is rejected because another machine pushed first, devkit rebases on it and pushes once more. A push that
still fails is recorded in the audit trail as a failed `handoff.push`, and `dev` and `PROJECTS.md` show it under
"Needs attention" until a push succeeds. [Troubleshooting](troubleshooting.md#the-handoff-repository-does-not-push)
has the fixes.

## Merge rules

`dev install --only handoff` appends these lines to the handoff repository's `.gitattributes` and sets the `ours`
merge driver:

| Path | Merge rule | Why |
| --- | --- | --- |
| `sessions/**` | `union` | Append-only logs: both machines' lines are kept |
| `presence/*.json` | `ours` | Each machine writes only its own file |
| `PROJECTS.md`, `TASKS.md`, `ACTIVITY.md`, `registry.json` | `ours` | Generated views: the local copy is kept and regenerated after the pull |

Work items are separate files, and syncs pull with `--rebase --autostash` before they push, so conflicts are rare. When
two machines change the same item at the same moment, devkit settles it item by item: it takes the version pushed
first and applies your change to it again. Each field, the title, and the description take the side that changed
them, the links keep what either side added, and your changelog lines go on top of the other machine's, numbered after
them. Two changes to the same thing, such as a lock taken on both machines or a description edited on both, cannot be
settled that way: a claim reads the item again and is refused with the holder's name, and any other change stops the
sync as described next. On this Mac, devkit runs its pulls, commits, and pushes of the handoff repository one at a
time.

### The handoff repository stopped syncing

A pull that stops on a conflict leaves git in the middle of a rebase. Before this was handled, every later pull,
commit, and push then failed without a word, and a commit made in that state was lost. Now devkit:

1. Ends a rebase it finds stopped (`git rebase --abort`) before any read of the items, pull, or commit, and when
   `dev sync` or `dev install` runs. Your commits come back as they were.
2. Ends at once a pull whose conflict it cannot settle: a file other than a work item, such as a project note, or an
   item whose same field or description both machines changed. Your commits stay on your Mac, unpushed.
3. Records each of these as a sync problem: a `handoff.rebase-aborted` record in the audit trail, kept at every audit
   level, with the files in conflict as its target, and the problem under "Needs attention" in `dev status` and the
   dashboard until a push succeeds.

To recover from a conflict devkit cannot settle, resolve it by hand in the handoff repository, then sync:

```bash
cd ~/Development/.handoff
git pull --rebase            # stops on the conflict again
git status                   # the files in conflict
$EDITOR <file>               # keep the lines both sides need, remove the conflict markers
git add <file>
git rebase --continue
dev sync
```

`dev audit --action handoff.rebase-aborted` lists every rebase devkit ended, with the files in conflict.

## What the guard protects

| Action | Decision |
| --- | --- |
| Writing a file under `items/`, `sessions/`, or `presence/` | Denied: use `dev task` or `dev handoff --log` |
| Writing `PROJECTS.md`, `TASKS.md`, `ACTIVITY.md`, `registry.json`, or `log/` | Denied: `dev sync` generates them |
| Writing a file under `policies/` | Denied: use `dev delivery apply` |
| Writing a file under `lessons/` | Denied: use `dev lesson add\|recur\|prevent\|retire` |
| Writing another project's note or plans from a project session | Denied |
| Writing an initiative the session's project is not a member of | Denied |
| `dev task -p <other project>` or `-i <initiative it is not in>` from a project session | Denied |
| `dev initiative new\|add\|rm` from a project session | Denied: membership is a portfolio decision |

A portfolio-level session (started in `~/Development` or in the handoff repository) may work across projects when you
ask it to. The full rules are in [Guard rules](guard.md#the-handoff-repository).

See also [Initiatives](initiatives.md), [Progress and communication](progress-and-communication.md),
[Work states](work-states.md), and [Commands › dev task](commands.md#dev-task--p-project---i-slug---all-verb-).
