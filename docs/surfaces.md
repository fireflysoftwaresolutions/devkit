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
| Crew | Launches members as `devkit:<id>` agents; checks limits before launch | The same | Records role launches at SubagentStart, binds steps, and counts slots. An over-limit launch is restricted after it starts; the host supplies no pre-launch hook | The same; registration and step binding observed live | Shows the crew, who works now, and decisions waiting (`devkit_crew`); launches no one |
| Session-start welcome (what needs you, what runs, what is next, where you are) | Yes, shown as the hook's message when the session opens | Sent the same way; how the app shows it is not verified yet | Sent at your first prompt, just before the first reply (Codex runs SessionStart at the first turn); how the terminal UI shows it is not verified yet | Sent the same way; how the app shows it is not verified yet | No |
| Routing reminder (declare each request's kinds of work) | Yes, the same reminder in every language | Yes | Yes | Yes | No; `devkit_rules` returns the routing rules on request |
| Required skills and known pitfalls in crew briefs | Yes, added to every brief | Yes | `dev crew brief` names them; SubagentStart also returns required skills, known pitfalls, and the report format | The same; required-skill context observed in this app investigation | Not applicable |
| Crew gates when a member stops | Yes: status block, `dev progress step`, then proofs, skills, unfinished work, tests, and lessons | Yes | Implemented at SubagentStop for recorded launches; covered by hook contract tests | Live SubagentStop sent a member back for missing `dev progress step`; other gates have contract coverage | Not applicable |
| After the context is compacted | PreCompact keeps a checkpoint and tells the summarizer what to keep; SessionStart gives the continuity brief (the run, the crew working now, what waits, your latest requests) before the workspace context; the next hook names what the summary left out | The same | The checkpoint and the brief (`compact` source); Codex sends no summary to check. Verified on the payloads Codex CLI 0.167 sends | As in the CLI | `devkit_resume` gives the same brief, with your request text only when the chat is the session's own provider |
| Sessions on one Mac: see the others, talk, claim, escalate ([Teams and machines](teams.md#sessions-on-one-mac)) | Every `dev session` command; a background `dev watch --wait` wakes the session for other sessions' messages | The same | Every `dev session` command; use a bounded foreground wait and resume its process if the host yields | The same; `--timeout` is in minutes, so `--timeout 0.1` waits up to six seconds | No |
| Status block checked and scored | Implemented at Stop for development turns | The same | Implemented; contract tests cover scoring and send-backs | Projectless app sessions exposed a scoring gap; see the evidence matrix below | Asked for, not checked |
| Writes code | Yes | Yes | Yes | Yes | No |
| Verified in real sessions | Terminal: yes. IDEs: none recorded yet | Yes | Partly, with Codex CLI 0.167.0 (see below) | Partly, on November 10, 2026; gaps and evidence below | The tools have tests; `dev desktop status` checks the install |

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
| Known gaps | Codex supplies no hook before `collaboration.spawn_agent`, so an over-limit launch can only be restricted after it starts. devkit does not register Codex's Interrupt and PermissionRequest events. Projectless app sessions exposed the context and scoring gaps described below |
| Not run yet | The welcome as the terminal UI shows it (`codex exec` shows none); the global instructions (`~/.codex/AGENTS.md`) in a session of this release; lesson recall on matching commands; grants and approvals typed at the prompt (`grant …`, `approve D<n>`); `dev watch --wait` in the foreground past the 30 seconds after which Codex's `exec` tool hands control back |

The app investigation on November 10, 2026 used devkit 26.11.10 in a chat started outside a repository, with
repository commands sent through `functions.exec` to `tools.exec_command` and agents launched through
`collaboration.spawn_agent`. Rollout metadata identified bundled Codex `0.167.0-alpha.3.2`. Raw hook capture was
off: rollout and audit records corroborate normalized `Bash` calls and a patch inside `tools.apply_patch`, not the
app's original hook envelopes. An audit row's crew tool identifier can identify the launch, so it is not by itself
the patch call identifier. The existing 0.167 contract shapes are replay evidence, not newly captured raw app events. These
observations do not prove that every app version emits the same events.

### Evidence matrix

**Live** means observed in a running session. **Contract** means an isolated test of a handler or recorded event
shape; it is not a live host test. **Code** means implementation inspection. **Unverified** means no end-to-end
observation was recorded for that claim. The Claude column describes the reference behavior, not a new Claude
session run during this app investigation.

| Area | Claude reference behavior | Codex evidence and limits |
| --- | --- | --- |
| Instructions and skills | Global instructions and skill rules reach the main session and crew | Live: global instructions and the skills catalog reached this app chat; a member received required-skill context. Code: [launch context](../lib/devkit/codexlaunch.py) supplies skills at SubagentStart. Loading instructions alone does not prove compliance |
| Project and working directory | Session context identifies the repository and branch | Live gap in 26.11.10: a projectless chat kept an empty project and branch. Contract: [projectless tests](../tests/test_codex_projectless.py) bind the registered root to its managed checkout after an executed call or a progress run, including short tasks, executed failures, and deferred results; the original host directory stays intact. Rejected, child, or foreign calls cannot replace the binding |
| Session routing | Commands and crew messages belong to their calling session | Contract: [session routing tests](../tests/test_codex_session_routing.py) preserve the root identity through nested and background commands, even when roots share a process. Ambiguous, contradictory, or foreign identity is refused. Paired tests retain Claude's existing selection behavior |
| Pre-tool guard | Checks commands and file writes before they run | Live: a member's lead-only command was denied, with a `guard.deny` record. The [Codex shell payload contracts](../tests/test_hooks_codex.py) cover `exec_command` and its `workdir`. This does not prove every tool exposed by the app has a guard hook |
| Canary and Stop | Checks the final development reply and records its score | Live gap in 26.11.10: projectless chats had no scored rows. Contract: [projectless tests](../tests/test_codex_projectless.py) exercise the real Stop handler using the session's own registered repository, excluding unrelated sessions and non-development turns. An empty report is not a pass |
| Crew registration and limits | Checks launch limits before an Agent call; binds the returned member | Live: `crew.start` and `crew.bind` recorded the app launch and its step. Contract: [launch tests](../tests/test_codex_launch.py) cover binding; [projectless tests](../tests/test_codex_projectless.py) cover valid readmission after a refusal. [Admission tests](../tests/test_codex_admission.py) characterize matching readiness and admission under the same state; they did not reproduce a new defect. Codex restricts an over-limit member after launch; it cannot prevent that launch |
| Scratch and file scope | Each member uses its own scratch folder and assigned repository files | Contract: [app scratch](../tests/test_codex_scratch.py) and [CLI scratch](../tests/test_codex_cli_scratch.py) tests execute reviewer probes and reject foreign sessions, members, linked paths, and member access to the lead's files. Repositories inside scratch retain project scope. See [Codex scratch folders](guard.md#codex-scratch-folders) |
| Concurrent edits | The write ledger distinguishes certain and possible authorship | Contract: [attribution tests](../tests/test_codex_attribution.py) register accepted patches before execution, give failed patches no exact credit, and limit possible authorship to the reader's call window and canonical repository. [Patch scope tests](../tests/test_codex_patch_scope.py) cover literal patch headers, spaces, and move destinations. Historical attribution flags are not silently erased |
| Crew closure, tests, and approvals | Member reports owe status, progress evidence, procedure checks, and accountable review | Live: the SubagentStop hook sent the Technical writer (Ghost) back for a missing `dev progress step` command despite a formatted report. Contract: [member-stop tests](../tests/test_hooks_codex.py) and [parity tests](../tests/test_codex_parity.py) exercise other report checks and workflow rules. A full live app cycle through every gate remains unverified |
| Messages and waits | Crew and peer messages share devkit's wait; Claude supports background command notifications | Live: hooks delivered questions and answers, but a wait consumed a truncated reply. Contract: [watch tests](../tests/test_codex_watch.py) retain complete sanitized stored messages, acknowledge each only after output, and keep audit events for moot questions. Documented timeout or empty-watch outcomes are control flow; argument errors and real CI failures stay failures. Native notifications do not drain devkit's queue. Timeout units are minutes |
| Continuity and compaction | Keeps the run, crew, decisions, and requests through compaction | Live: run and crew survived compaction, but the installed version omitted mid-turn Codex requests. Contract: [steering tests](../tests/test_codex_steering.py) read human `UserMessage` records from the root's validated interactive rollout, including failed and deferred tools, without granting approval. Read-only replay recovered the actual root's requests; this is not an installed host test or a test of the app's summarizer |
| Audit | Records launch, binding, refusals, gates, and completion under the session | Live: the app wrote `crew.start`, `crew.bind`, and `guard.deny`. Searching only `crew.launch` misses the actual lifecycle names. `crew.gate` is a findings event; its absence alone does not prove that a successful gate ran |
| Permissions and approvals | Typed decisions and grants authorize their named actions | The [Codex grant contracts](../tests/test_codex_grants.py) reject copied, replayed, and wrong-session evidence. The app's complete approval flow remains unverified. No grants were issued to test it |
| Extensions and installation | Managed changes use the developer's authorized installation mechanism | Live: extension drift and acceptance notices appeared. No extension acceptance, permission change, or global installation was performed. A checkout fix does not change the pinned hooks of a running session |

After installing a verified correction through the supported developer workflow, start a new session and check
project identity, a recorded development-turn score, crew binding, and gate evidence again. Until then, keep source
test results separate from live verification of the installed version. See
[Trying a change in your own sessions](development.md#trying-a-change-in-your-own-sessions).

The app's artifact and worktree views are separate from devkit's crew records. In this investigation, the artifact
list was empty and the app refused to attach a worktree created by `dev wt` because its own registry did not manage
it. Native agent listings and devkit's launch records still identified real members. No registry was changed to
make the worktree appear. Treat host attachment and display behavior as a separate, unverified integration boundary.

If an older version recorded a concurrent reader as a certain writer, retain the original evidence and reconcile
the flag through the supported work-state workflow or a fresh review. The attribution fix does not prove that
every historical flag was wrong.

Claude Code was checked in real sessions in the terminal and in Claude Desktop's Code tab: each session's status
block results are recorded per surface (`dev canary`). The VS Code extension and JetBrains IDEs run the same Claude
Code on the same `~/.claude` settings and hooks, but no session from them was recorded yet.

## Surfaces devkit does not reach

- **Cloud and SSH sessions** of Claude Desktop's Code tab, and **Codex cloud tasks**, run on another computer. They do
  not load the configuration on your Mac, so devkit's hooks, guard, and crew are not there. Choose a **Local**
  session for devkit.
- **Claude on the web and the mobile apps** do not run Claude Code on your Mac.

## How to tell that devkit is active

On development surfaces, the agent's final reply should start with the status block: a bold
`[Firefly> DevKit]` line followed by the **Done**, **Check**, and **Next** bullets. This is a visible instruction
check. It does not prove that hooks or gates ran. A missing block can mean a loading problem or an agent that did not follow
loaded instructions. Compare `dev doctor`, `dev canary`, the session record, and relevant audit events. See
[Concepts › The status block](concepts.md#the-status-block).

See also [Agents](agents.md), [The crew](crew.md), and [Guard rules](guard.md).
