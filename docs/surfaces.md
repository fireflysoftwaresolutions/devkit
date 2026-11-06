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
| Hooks | All ten events | All ten events | Eight events, after Codex trusts them | The same eight events | None |
| Guard | Denies, or asks you to confirm | Denies, or asks you to confirm | Denies (Codex has no "ask") | Denies | No guard hook; its two task tools run `dev task`, which checks and audits each change as in a terminal |
| Crew | Launches members as `devkit:<id>` agents | Launches members as `devkit:<id>` agents | Launches members as roles (`spawn_agent`) | Launches members as roles | Shows the crew, who works now, and decisions waiting (`devkit_crew`); launches no one |
| Routing hint (Morpheus's route for each request) | Yes | Yes | Yes | Yes | No; `devkit_rules` returns the routing rules on request |
| Required skills in crew briefs | Yes, added to every brief | Yes | Not by the hook, because Codex lets a hook change only shell commands; the session context and `dev crew brief` name the skills instead | As in the CLI | Not applicable |
| Status block checked and scored | Yes, at the end of every development turn | Yes | Yes | Yes | Asked for, not checked |
| Writes code | Yes | Yes | Yes | Yes | No |
| Verified in real sessions | Terminal: yes. IDEs: none recorded yet | Yes | **Not yet** (see below) | **Not yet** (see below) | The tools have tests; `dev desktop status` checks the install |

The ten hook events of Claude Code are SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, SubagentStart,
SubagentStop, Stop, SessionEnd, ConfigChange, and InstructionsLoaded. Codex has no ConfigChange or InstructionsLoaded
event. [Agents › Hooks](agents.md#hooks) says what each one does.

### What "not yet" means for Codex

devkit's live test of Codex (`dev agents test codex`, a short real session) waits for the Codex usage quota of the
test account to reset on **2026-11-09**. Until then, the Codex columns of the table come from devkit's code, its tests, and
the configuration that `dev doctor` checks (hook trust, plugins, roles, and the ChatGPT app's bundled Codex). They do
not come from a recorded Codex session. If Codex behaves differently on your Mac, `dev doctor` and `dev canary` show
it, and [Troubleshooting](troubleshooting.md#codex-hooks-are-not-running) has the fixes.

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
