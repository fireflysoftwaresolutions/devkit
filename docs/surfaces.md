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
| Crew | Launches members as `devkit:<id>` agents | Launches members as `devkit:<id>` agents | Launches members as roles (`spawn_agent`); devkit does not record the launch yet, so crew slots, the depth limit, and step binding do not apply | Launches members as roles, as in the CLI | Shows the crew, who works now, and decisions waiting (`devkit_crew`); launches no one |
| Session-start welcome (what needs you, what runs, what is next, where you are) | Yes, shown as the hook's message when the session opens | Sent the same way; how the app shows it is not verified yet | Yes, after your first prompt, just before the first reply (Codex runs SessionStart at the first turn) | Sent the same way; how the app shows it is not verified yet | No |
| Routing reminder (declare each request's kinds of work) | Yes, the same reminder in every language | Yes | Yes | Yes | No; `devkit_rules` returns the routing rules on request |
| Required skills and known pitfalls in crew briefs | Yes, added to every brief | Yes | Not by the hook, because Codex lets a hook change only shell commands; `dev crew brief` names them instead | As in the CLI | Not applicable |
| Crew gates when a member stops | Yes: status block, `dev progress step`, then proofs, skills, unfinished work, tests, and lessons | Yes | **Not yet verified:** the checks read the member's reply or transcript, and no recorded Codex payload carries them yet (see below) | As in the CLI | Not applicable |
| After the context is compacted | PreCompact keeps a checkpoint and tells the summarizer what to keep; SessionStart gives the continuity brief (the run, the crew working now, what waits, your latest requests) before the workspace context; the next hook names what the summary left out | The same | The checkpoint and the brief (`compact` source); Codex sends no summary to check. **Not yet verified** live | As in the CLI | `devkit_resume` gives the same brief, with your request text only when the chat is the session's own provider |
| Status block checked and scored | Yes, at the end of every development turn | Yes | Yes | Yes | Asked for, not checked |
| Writes code | Yes | Yes | Yes | Yes | No |
| Verified in real sessions | Terminal: yes. IDEs: none recorded yet | Yes | **Not yet** (see below) | **Not yet** (see below) | The tools have tests; `dev desktop status` checks the install |

The thirteen hook events of Claude Code are SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,
PostToolUseFailure, SubagentStart, SubagentStop, Stop, SessionEnd, ConfigChange, InstructionsLoaded, PreCompact, and
PostCompact. Codex has no PostToolUseFailure, ConfigChange, or InstructionsLoaded event. [Agents › Hooks](agents.md#hooks) says what each one
does.

### What "not yet" means for Codex

devkit's live test of Codex (`dev agents test codex`, a short real session) waits for the Codex usage quota of the
test account to reset on **2026-11-09**. Until then, the Codex columns of the table come from devkit's code, its tests, and
the configuration that `dev doctor` checks (hook trust, plugins, roles, and the ChatGPT app's bundled Codex). They do
not come from a recorded Codex session. If Codex behaves differently on your Mac, `dev doctor` and `dev canary` show
it, and [Troubleshooting](troubleshooting.md#codex-hooks-are-not-running) has the fixes.

The crew's checks at SubagentStop are the one gap the code already shows. Codex registers the event, but the checks
read the member's final reply or its transcript, and devkit has no recorded Codex payload that carries either. Until
the live test confirms it, treat a Codex member's report as unchecked: read its status block and its work yourself.

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
