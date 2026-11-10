# Where to use devkit: the surfaces compared

devkit works in six places, called surfaces. This page says what each surface is for, what devkit does on it, and
which tutorial to follow. Install devkit first ([Getting started](getting-started.md)): every surface below reads the
configuration that the installer writes.

## Pick a surface

| You want to | Use | Tutorial |
| --- | --- | --- |
| Build or change code in a terminal window | Claude Code in Terminal, or in VS Code or a JetBrains IDE | [Claude Code](tutorials/claude-code.md) |
| Build or change code in an app with buttons and menus | The **Code** tab of Claude Desktop | [Claude Desktop's Code tab](tutorials/claude-desktop.md) |
| Build or change code with OpenAI's agent, in a terminal | Codex CLI | [Codex CLI](tutorials/codex-cli.md) |
| Build or change code with OpenAI's agent, in an app | Codex in the ChatGPT desktop app | [Codex in the ChatGPT app](tutorials/chatgpt-codex.md) |
| Ask where things stand, plan, or write a report, without changing code | Claude Desktop's chat or Cowork | [Claude Desktop chat and Cowork](tutorials/desktop-chat-and-cowork.md) |

In Claude Desktop, development happens in the **Code** tab. That is where devkit's guard, hooks, and crew run. Chat
and Cowork read devkit's data through the devkit extension, so they can report and plan, and they send code changes to
the Code tab.

## What each surface can do

| | Claude Code in Terminal and IDEs | Claude Desktop's Code tab | Codex CLI | Codex in the ChatGPT app | Claude Desktop chat and Cowork |
| --- | --- | --- | --- | --- | --- |
| Reads its setup from | `~/.claude` | `~/.claude` (Local sessions only) | `~/.codex`, `~/.agents/skills` | `~/.codex` | The devkit extension |
| Hooks | All thirteen events | All thirteen events | Ten events, after Codex trusts them | The same ten events | None |
| Runs devkit from | The pinned copy that `dev install` made | The pinned copy | The pinned copy | The pinned copy | The pinned copy, through the extension's launcher |
| Guard | Denies, or asks you to confirm | Denies, or asks you to confirm | Denies (Codex has no "ask") | Denies | No guard hook; its two task tools run `dev task`, which checks and audits each change as in a terminal |
| devkit's own state and configuration | The guard refuses agents' writes to them | The same | The same, for shell commands and `apply_patch` | As in the CLI | Not applicable: its tools write only through `dev task` |
| Crew | Launches members as `devkit:<id>` agents | Launches members as `devkit:<id>` agents | Launches members as roles (`spawn_agent`). Codex fires no hook before a launch, so devkit does not record it: crew slots, the depth limit, and step binding do not apply | Launches members as roles, as in the CLI | Shows the crew, who works now, and decisions waiting (`devkit_crew`); launches no one |
| Session-start welcome (what needs you, what runs, what is next, where you are) | Yes, shown as the hook's message when the session opens | Sent the same way; how the app shows it is not verified yet | Sent at your first prompt, just before the first reply (Codex runs SessionStart at the first turn); how the terminal UI shows it is not verified yet | Sent the same way; how the app shows it is not verified yet | No |
| Routing reminder (declare each request's kinds of work) | Yes, the same reminder in every language | Yes | Yes | Yes | No; `devkit_rules` returns the routing rules on request |
| Required skills and known pitfalls in crew briefs | Yes, added to every brief | Yes | Not by a hook, because Codex fires none before a launch, and devkit does not yet add them when the member starts; `dev crew brief` names them instead | As in the CLI | Not applicable |
| Crew gates when a member stops | Yes: status block, `dev progress step`, then proofs, skills, unfinished work, tests, and lessons | Yes | **No.** Codex sends the member's final reply and transcript at SubagentStop, but the checks apply to a member devkit saw launched, and it records no Codex launch. Read a Codex member's status block and its work yourself | As in the CLI | Not applicable |
| After the context is compacted | PreCompact keeps a checkpoint and tells the summarizer what to keep; SessionStart gives the continuity brief (the run, the crew working now, what waits, your latest requests) before the workspace context; the next hook names what the summary left out | The same | The checkpoint and the brief (`compact` source); Codex sends no summary to check. Verified on the payloads Codex CLI 0.167 sends | As in the CLI | `devkit_resume` gives the same brief, with your request text only when the chat is the session's own provider |
| Sessions on one Mac: see the others, talk, claim, escalate ([Teams and machines](teams.md#sessions-on-one-mac)) | Every `dev session` command; a background `dev watch --wait` wakes the session for other sessions' messages | The same | Every `dev session` command; `dev watch --wait --timeout 10` in the foreground, or the messages at the next prompt | As in the CLI | No |
| Status block checked and scored | Yes, at the end of every development turn | Yes | Yes | Yes | Asked for, not checked |
| Writes code | Yes | Yes | Yes | Yes | No |
| Verified in real sessions | Terminal: yes. IDEs: none recorded yet | Yes | Partly, with Codex CLI 0.167.0 (see below) | **Not yet** (see below) | The tools have tests; `dev desktop status` checks the install |

The thirteen hook events of Claude Code are SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,
PostToolUseFailure, SubagentStart, SubagentStop, Stop, SessionEnd, ConfigChange, InstructionsLoaded, PreCompact, and
PostCompact. Codex has no PostToolUseFailure, ConfigChange, or InstructionsLoaded event. [Agents › Hooks](agents.md#hooks) says what each one
does.

### What was verified on Codex

devkit was checked with Codex CLI 0.167.0 on November 9, 2026: in live `codex exec` sessions, through the hook
payloads and rollout records captured from them, and by devkit's acceptance tests, which run those payloads through
the real hook entry. The live sessions found failures (the Stop checks skipped every Codex turn, commands inside
Codex's `exec` tool went unseen, a patch could switch hooks off, and failed commands counted as successes); this
release fixes them, and the acceptance tests confirm the fixes on the same payloads.

| Result | What |
| --- | --- |
| Works | The workspace context at session start; the routing reminder at each prompt; the guard on shell commands, with the commit attribution check and the push gate; the guard on `apply_patch` writes to agent settings files; the status block check at Stop and every Stop send-back; skills (all visible, a skill load seen, the rules on changed files and on commands); a member's start and stop; the patch-loop stop and the bug registry's ledger for failed commands; Codex calls audited under Codex and its own session; PreCompact and PostCompact |
| Known gaps | Crew launches are not recorded, because Codex fires no hook before `spawn_agent`, so crew slots, the depth limit, step binding, and the checks when a member stops do not apply. A member's required skills are not added to its brief. devkit does not register Codex's Interrupt and PermissionRequest events |
| Not run yet | The welcome as the terminal UI shows it (`codex exec` shows none); the global instructions (`~/.codex/AGENTS.md`) in a session of this release; lesson recall on matching commands; grants and approvals typed at the prompt (`grant …`, `approve D<n>`); `dev watch --wait` in the foreground past the 30 seconds after which Codex's `exec` tool hands control back |

The ChatGPT app runs its own bundled Codex on the same `~/.codex` configuration, but no session in the app was
recorded yet. Its column comes from these CLI results, devkit's code, and what `dev doctor` checks (hook trust,
plugins, roles, and the version of the app's bundled Codex). If Codex behaves differently on your Mac, `dev doctor`
and `dev canary` show it, and [Troubleshooting](troubleshooting.md#codex-hooks-are-not-running) has the fixes.

Claude Code was checked in real sessions in the terminal and in Claude Desktop's Code tab: each session's status
block results are recorded per surface (`dev canary`). The VS Code extension and JetBrains IDEs run the same Claude
Code on the same `~/.claude` settings and hooks, but no session from them was recorded yet.

## Surfaces devkit does not reach

- **Cloud and SSH sessions** of Claude Desktop's Code tab, and **Codex cloud tasks**, run on another computer. They do
  not load the configuration on your Mac, so devkit's hooks, guard, and crew are not there. Choose a **Local**
  session for devkit.
- **Claude on the web and the mobile apps** do not run Claude Code on your Mac.

## How to tell that devkit is active

On every surface, the agent's final message of a development turn starts with the status block: a bold
`[Firefly> DevKit]` line followed by the **Done**, **Check**, and **Next** bullets. A reply without it means the
session did not load devkit's instructions. Restart the session, then run `dev doctor` in Terminal. See
[Concepts › The status block](concepts.md#the-status-block).

See also [Agents](agents.md), [The crew](crew.md), and [Guard rules](guard.md).
