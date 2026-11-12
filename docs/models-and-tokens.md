# Models and tokens

This guide explains how devkit helps agents spend models, reasoning effort, subagents, and tokens where they buy
quality: what it configures in Claude Code and Codex, what it deliberately leaves to you, and the rules agents follow.

Quality is the constraint and tokens are the budget. The cheapest run is the one that is right the first time: a
wrong fix or a red CI run costs more than any tokens saved on the way.

## The main model is yours

devkit never sets the main model or its effort, in either agent. You choose them with `/model` (and
`/effort` in Claude Code), and they stay your choice:

- **Your plan and your judgment decide.** Which models your account offers, what they cost you, and how much
  reasoning a task deserves are decisions for the person who pays and reviews the work.
- **Switching costs more than it saves.** Each mid-session switch of model (or of Claude's fast mode) re-reads the
  whole conversation without the prompt cache. Agents are told not to switch on their own and to say so in one line
  when a task needs a different tier.
- **Subagents inherit the session model.** This is the vendor default in both tools and devkit keeps it, so
  delegated work is as good as the main session unless a smaller model is chosen on purpose for checkable lookups.
  Crew members add only effort: `strong` members run at high effort (in Codex, on the `deep` profile's model).

## What devkit configures

| Setting | Claude Code | Codex |
| --- | --- | --- |
| A small read-only agent for lookups | The Lookup scout (Sparks) (`devkit:sparks`, alias `devkit:scout`) | The `sparks` role and its `scout` alias in `~/.codex/agents/` |
| Profiles for other tiers | Not needed: `/model` and `/effort` | `fast`, `deep`, and `review` in `~/.codex/<name>.config.toml` |
| Subagent model and effort | Inherit the session (optional override) | Inherit the session (optional override) |
| Parallel subagents | — | At most 4 threads per session |
| Fast mode | Opt in per session (`fastModePerSessionOptIn`) | — |

### The Lookup scout (Sparks)

A lookup whose answer you can check cheaply (where a symbol is used, which file sets a value, which log line failed)
does not need the session's model. The Lookup scout (Sparks) answers with `path:line` references and short excerpts,
says "not found" rather than guess, marks what it inferred, and never edits. `scout` is an alias for the Lookup scout in
both agents, so briefs that name the scout still work.

| | Claude Code | Codex |
| --- | --- | --- |
| Defined in | `config/crew.json`, generated into `plugin/agents/sparks.md` and `scout.md` (installed with the plugin) | `config/crew.json`, written to `~/.codex/agents/sparks.toml` and `scout.toml` by `dev install --only codex` |
| Model and effort | Haiku, low effort | The `fast` profile's model and effort (`gpt-6-luna`, low) |
| Tools or sandbox | `Read`, `Grep`, `Glob`; it does not load `CLAUDE.md` | Read-only sandbox |

The Lookup scout is the only `fast` member of the crew; the others use the session's model (see
[The crew › Who is on the crew](crew.md#who-is-on-the-crew)).

### Codex profiles

`codex --profile <name>` layers `~/.codex/<name>.config.toml` over your configuration for one session. devkit writes
three:

| Profile | Model | Reasoning effort | Use it for |
| --- | --- | --- | --- |
| `fast` | `gpt-6-luna` | low | Quick, mechanical sessions |
| `deep` | `gpt-6-astra` | high (plan mode: xhigh) | Hard debugging, design, risky changes |
| `review` | `gpt-6-astra`, also the `review_model` | high | Reviews, in a read-only sandbox |

`dev doctor` checks that the profile models are available on your account. Change or remove profiles with
`codex.profiles` (see the next section).

### Codex subagent threads

devkit sets `agents.max_concurrent_threads_per_session = 4` in `~/.codex/config.toml`. More parallel threads rarely
help (each one is a full context) and they use up plan limits quickly. Change it with `codex.max_subagent_threads`.

### Claude Code fast mode

`fastModePerSessionOptIn: true` makes fast mode a choice you make per session (`/fast`): it does not carry over to new
sessions. Fast mode runs the same model at a higher price, so it is for latency-critical interactive work, switched
on at the start of a session. devkit keeps this key out of the managed-settings lock on purpose, because a managed
value would stop `/fast` from working outside a terminal. Set `claude.fast_mode_per_session: false` to leave the key
out of your settings.

## Configuration keys

All optional, in `~/.config/devkit/config.json`. Run `dev install --only claude` or `dev install --only codex` after
a change.

| Key | Default | Effect |
| --- | --- | --- |
| `claude.subagent_model` | empty | When set, `CLAUDE_CODE_SUBAGENT_MODEL` in Claude's `env`: the model for every subagent. Empty keeps the session model (recommended) |
| `claude.subagent_effort` | empty | When set, `CLAUDE_CODE_SUBAGENT_EFFORT`: the effort for every subagent |
| `claude.fast_mode_per_session` | `true` | `false` leaves `fastModePerSessionOptIn` out of the managed settings |
| `codex.max_subagent_threads` | `4` | `agents.max_concurrent_threads_per_session` |
| `codex.profiles` | the three profiles | An object adds profiles or replaces a profile of the same name, for example `{"fast": {"model": "<model>", "model_reasoning_effort": "low"}}` with a model from `codex debug models`; `false` writes none |
| `codex.subagent_model` | empty | When set, `agents.default_subagent_model` |
| `codex.subagent_effort` | empty | When set, `agents.default_subagent_reasoning_effort` |

devkit owns the profile and role files it writes. Removing a profile from the configuration does not delete its file;
delete `~/.codex/<name>.config.toml` yourself.

## Token-light commands

Reading a whole tree, a long search result, or a full diff costs context that the next turns pay for again. devkit's
file commands give agents the same facts in fewer tokens, with every list capped and an explicit `+N more — narrow
with …` line instead of silent truncation:

| Instead of | Use | What changes |
| --- | --- | --- |
| `ls -R`, `tree` | `dev files [path] -d 2` | Folders first, counts and sizes, git markers, build output and `.git` folded |
| `find` | `dev find <pattern>` | Respects `.gitignore`, smart case, filters by extension, change, age, and size |
| `grep -r` | `dev grep <regex>` | `file:line: text`, trimmed to one line, five matches per file, structural search with `--ast` |
| `git diff` | `dev diff` | Changed files first, one line of context, and lock and generated files folded |
| `rm`, `mv`, `cp` | `dev rm`, `dev mv`, `dev cp` | Safe and recoverable: the Trash, `dev rm --undo`, no silent overwrite |

Every command accepts `--json` where a structured answer helps, and the details are in [Commands › Files](commands.md#files).

## The rules agents follow

The `working-efficiently` skill, and a short section of the global instructions, tell both agents how to spend
tokens. In short:

| Work | Model and effort |
| --- | --- |
| Architecture, trade-offs, ambiguous requirements | The session model; suggest the strongest for big calls; high effort |
| Debugging without a clear reproduction, concurrency, code you did not write | The session model, high effort (higher when stuck) |
| Security, auth, money, personal data, migrations, public API changes | The session model or stronger, never smaller; high effort |
| Final review before "done" or a PR | A fresh context at the session model, high effort |
| Routine implementation with a clear scope | The session model at its default effort |
| Searches and lookups, log triage | The Lookup scout (Sparks), low effort, only when the result can be checked |
| Mechanical edits from a pattern | A tool first (formatter, LSP rename, codemod); otherwise a small agent |

- **Downgrade only what you can verify cheaply.** A judgment, an unverifiable summary or "no issues found" from a
  small model proves nothing.
- **Delegate for independence or volume, not for intelligence.** Subagents pay off for broad searches, two to four
  independent questions, verbose output (full test runs, long logs), an independent review, and disjoint write
  sets. Multi-agent runs use many times the tokens of a single session. For Codex, the skill is the explicit
  instruction its spawn tool requires before it starts subagents.
- **Write a full brief and check what comes back.** Goal, scope, what is known, the return format; spot-check cited
  lines and run the tests yourself.
- **Escalate instead of retrying.** After two failed attempts at the same problem, stop, form a new hypothesis, raise
  effort, and then propose a stronger model to the developer.
- **Keep the context lean.** One task per context, compact at natural breaks, targeted reads, compact command output,
  independent calls batched in one message, long jobs in the background, and no waiting in loops.

The skill also has the briefing templates (lookup, worker, reviewer) and the tool notes for both agents.

See also [Agents](agents.md), [Skills and plugins](skills-and-plugins.md), and [Configuration](configuration.md).
