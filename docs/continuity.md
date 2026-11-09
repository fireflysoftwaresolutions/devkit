# Continuity: compaction, resume, and /clear

When a conversation grows too long, Claude Code and Codex compact it: they replace it with a summary and go on. A
summary can drop what matters most, such as your exact request, a crew member that is still running, or a decision
that waits for you. This page explains what devkit keeps of a session, what it gives back after a compaction, a
resume, or `/clear`, and how to read that record yourself.

## What devkit keeps

devkit keeps one continuity record per session in its state folder (`~/.local/state/devkit/continuity/`), which
agents cannot write:

- **The checkpoint:** the run and its current and next steps, the crew members working now with their steps, the
  decisions and crew messages waiting, your last 5 requests, the last status block, and git (branch, HEAD,
  uncommitted and unpushed changes).
- **The compactions** devkit saw, up to 20, with their trigger and their tokens before and after.
- **"Not in the summary":** the items a summary left out, waiting for the next hook to hand them back.

Your requests are private to the session:

- Each is redacted with the audit trail's redactor and cut to 1,000 characters.
- It waits as pending until the Stop hook classes the turn, and is kept only when the turn was development work. When
  the classifier fails, it goes.
- Only the session's own provider sees its text: Anthropic for a Claude Code session, OpenAI for a Codex session.
  Any other reader gets the number of requests and their times.
- It is deleted 7 days after it was written, and `dev session forget` deletes it at once.

Background notifications, such as subagent results and system notices, are never stored. The summary itself is never
stored or injected. A record is deleted 7 days after its last change, so it outlives the session and
`claude --resume` still finds it.

## What happens at a compaction

| Moment | Claude Code | Codex |
| --- | --- | --- |
| Before the compaction (PreCompact, manual or automatic) | devkit writes the checkpoint and records the compaction. In the main session, it adds its own instructions to the summarizer's: keep every open request word for word, the run and its current step, the crew still running, the ids of the decisions and messages waiting, the branch and the release state, and the status block rule | devkit writes the checkpoint and records the compaction |
| After the compaction (PostCompact) | In the main session, devkit compares the summary with its record and keeps what the summary left out: running members, requests, decisions, messages, and the current step. It prints nothing | devkit closes the compaction's record. Codex sends no summary, so there is nothing to compare |
| The session goes on (SessionStart, source `compact`) | One line about the welcome you saw comes first, then the continuity brief, then the workspace context | The same |
| The next prompt or tool call | The main session gets "Not in the summary" once, first in the context, and the brief of a compaction that only the transcript showed | — |
| A crew member's own context is compacted | The member gets a reminder of its role, step, scope, procedure and where it was, the start of its launch brief, and its report format | Not verified yet |

Each hook fails open: when continuity fails, the hook answers as it would without it and writes the error to
`~/.local/state/devkit/logs/hooks.log`. A compaction is never blocked.

### The continuity brief

The brief is at most 4 KB. It holds, in this order:

1. Your latest requests, newest first, word for word.
2. The run: its goal, state, mode, current step, how many steps are done, and what comes next.
3. The crew working now, each member by role with its step and minutes, marked not to relaunch: its result arrives
   as a notification.
4. The decisions waiting, with their options and the recommendation, and the crew messages waiting: only the
   questions that nobody answered yet.
5. The last status block, so the next one keeps the task's title.
6. Git: the branch, HEAD, and the uncommitted and unpushed changes.
7. The rule: your final message starts with the status block.

When it does not fit, the brief leaves out, in this order: the text of decisions and messages (their ids stay), the
status block, the next steps, and the git details; then it cuts the older requests to 200 characters and drops the
oldest ones.

## Resume and /clear

- **Resume** (`claude --resume`, source `resume`): the session gets the same brief, from the record kept for 7 days.
- **`/clear`:** when the session ends, devkit links the next session started in the same folder to it, for 10
  minutes, and the requests it kept go. The new session gets a notice instead of a brief: the previous session's open
  run, its current step, and the members still running, whose results go to that session. To continue the run here,
  the tech lead runs `dev session carry`; to close it, `dev session carry` and then
  `dev progress done "<outcome>" --why "…"`. `carry` is refused while a member of that run is still running.

When a session ends with its run still open, devkit marks the run orphaned, so that no view shows it as running.
The run runs again in the session that works on it: when its own session resumes, or when `dev session carry` moves
it into the session `/clear` started. Its history records why (`run.running` in the audit trail). See
[Work states › Runs](work-states.md#runs).

## Read the record yourself

Where no hook runs, or to check what a session gets back, read the record with `dev session`. `brief`,
`carry`, and `forget` are the tech lead's: from a crew member's call, devkit refuses them.

```bash
dev session brief                             # what devkit kept for this session
dev session brief --latest                    # the newest session of the project with an open run or requests (12 hours)
dev session carry                             # after /clear: move the open run into this session
dev session compactions <transcript>          # count the compactions in a transcript; reads only
dev session forget --session claude:<id>      # delete the records of one session (--all: every record here)
```

`dev session compactions` lists each compaction with its time, trigger, and tokens:

```text
main.jsonl: 6 compaction(s)
  2026-11-05 20:24 · auto · 967k → 61k tokens
  2026-11-05 22:59 · auto · 968k → 50k tokens
```

In an agent session, the guard refuses a command that names a live transcript, so run `compactions` in your own
terminal, or on a copy in the session's scratch folder. Every option is in
[Commands › dev session](commands.md#dev-session-brief--carry--compactions--forget).

## In Claude Desktop chat and Cowork

Chat and Cowork run no hooks, so they get the brief through the devkit extension's read-only `devkit_resume` tool. It
returns the brief of a project's latest session with an open run or requests in the last 12 hours, each request cut
to 200 characters. Your request text shows only when the app is that session's provider, which the server takes from
the app's own connection, never from a tool argument; otherwise the tool gives their count and times. The extension
tells the model to call `devkit_resume` after a summary or a resume, before it reports or plans development work.

## What the agent is told

The global instructions (section 11, "When the conversation is compacted") tell the agent that devkit's record wins
over the summary and over what it remembers. It reads the brief and its "Not in the summary" list, re-reads what the
list names before it acts, and never relaunches a member that is still running. Where no hook runs, it gets the same
brief with `dev session brief` or `devkit_resume`. When it writes a summary, it keeps your open requests word for word,
with their paths, ids and versions, the run and the crew, the decisions waiting, and the release state.

## Limits

- **Codex** gets the checkpoint and the brief, but no "Not in the summary" check, because it sends no summary. The
  hooks were checked on the payloads Codex CLI 0.167 sends (see
  [Surfaces](surfaces.md#what-was-verified-on-codex)).
- **The brief is capped at 4 KB.** A long run with many members and requests loses text first, then its older
  requests.
- **Seven days.** After that, your requests are gone from the record, and a resumed session gets the run only.

See also [The crew](crew.md), [Agents › Hooks](agents.md#hooks), [Commands](commands.md), and
[The audit trail](audit.md).
