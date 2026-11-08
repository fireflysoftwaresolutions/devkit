# Developing devkit

This guide is for people who change devkit itself: the code layout, the design principles, the tests, and how a
release is made.

Maintained by Andrés Contreras Guillén. © 2026 Firefly Software Solutions Inc. Licensed under the Apache License 2.0;
see [LICENSE](../LICENSE).

## Code layout

```text
bin/dev                 entry point (adds lib/ to sys.path, calls devkit.cli.main)
lib/devkit/
  cli.py                argument parsing and command implementations
  core.py               paths, configuration and defaults, shell and git helpers, remote parsing, project ids
  ui.py                 terminal colors, the wordmark banner and tagline, headings, tables, status pills, status block
  tui.py                the terminal UI of the setup wizard: select, multiselect, confirm, text, spinner
  registry.py           project discovery and inspection, PROJECTS.md, TASKS.md, ACTIVITY.md, registry.json, notes
  notes.py              handoff and initiative notes: structure, frontmatter, sections, summaries
  work.py               work items, locks, versions, changelog, session logs, presence, the queued near-real-time sync
  plans.py              versioned plans and specs
  progress.py           dev progress runs, ETA, calibration, timing, notifications, progress hooks
  report.py             dev report and its health rules
  migrate.py            dev handoff-import
  hooks.py              hook entry points for every event
  guard.py              pre-tool workspace rules shared by Claude Code and Codex
  skillguard.py         required-skill rules: prompt reminders, subagent briefs, the Stop check
  canary.py             status block parsing, format score and canary reports
  policy.py             managed settings: diff, repair, the Claude Code lock, project overrides
  security.py           dev sec scan and the pre-commit secret check
  ci.py                 GitHub Actions workflow reader for dev ci
  cistatus.py           dev ci status: CI/CD runs on GitHub, GitLab, Bitbucket, and Azure, with an ETA
  ciwatch.py            dev ci watch: follow a run to its end and say why it failed and what to do
  pipelines.py          dev ci audit and dev ci init on the four CI/CD hosts
  crew.py               dev crew: the roster from config/crew.json, briefs, the session graph, team changes
  raci.py               the crew's RACI: who does, approves, is consulted on, and is informed about each kind of work
  dispatch.py           routing work to a crew member, delegation between members, dispatch of ready work
  gates.py              what a crew member owes before it stops (SubagentStop)
  selfcheck.py          the consistency checks: the parts of devkit that describe each other, compared
  completion.py         dev completion: zsh and bash completion generated from the parser, and its live values
  state.py              serialized, atomic writes of the session and run records
  scope.py              whether a turn is development work
  stubs.py              dev stubs: unfinished work in delivered code
  testx.py · testaudit.py   dev test: failed first, then the blast radius, dev test prove; dev test audit
  delivery.py           dev delivery: detect, recommend, and record a delivery policy; the tag check
  fs.py                 dev files, find, grep, diff, rm, mv, and cp: token-light and recoverable
  gitx.py               dev branch, commit, rebase, push, pr, run, release, issue, git, gh; local CI evidence
  lessons.py            dev lesson: record, recall, recur, prevent, signals, usage, strength, consolidation
  sleep.py              dev sleep: consolidate and forget lessons, trim devkit's own old records
  jobs.py               dev jobs: the scheduled launchd jobs, their journal, and notifications
  audit.py              dev audit: the audit trail, redaction, publishing to the handoff repository
  art/                  the Firefly logo, embedded in the dashboard
  gc.py                 safe disk reclamation
  docs.py               dev docs check
  diagram.py            dev diagram: layout, routing, rendering and validation
  dashboard.py          the HTML command center
  web/                  the dashboard page: brand themes, styles, markup, script, and one file per rebuilt section
  dashserver.py         the dashboard as a local, live, read-only server
  desktop.py · mcp.py   the Claude Desktop extension and the MCP server (dev mcp) it runs
  providers.py          Claude Code and Codex status, quota and self-tests
  setup.py              dev install (the pinned copy, then the steps) and dev doctor
  wizard.py             dev setup
  toolchain.py          git hosts and clouds: their CLIs, sign-in checks and Homebrew groups
  codexrpc.py           JSON-RPC client for codex app-server
agents/AGENTS.md        global instructions rendered for Claude Code and Codex
plugin/                 the devkit plugin: skills, slash commands, the crew's agents (generated), icon
desktop/                the Claude Desktop extension: manifest and launcher
config/                 Claude Code, Codex, git, docs, skill and skill-rule configuration applied by dev install
templates/              plan, task, pull request and handoff templates
shell/                  zsh integration and completion
tools/brand/            renders the icon, the plugin logo and the hero banners
docs/                   these guides, diagram specs (docs/diagrams) and images
tests/                  unit tests
install.sh · Brewfile   bootstrap and toolchain
.github/                devkit's own CI (Python 3.9 and 3.13 on macOS), PR template and Dependabot
```

## Design principles

- **Standard library only, Python 3.9 compatible.** `dev` runs with Homebrew Python or the macOS system
  `/usr/bin/python3`, with no virtualenv, or dependencies.
- **Hooks fail open and return fast.** Every hook handler catches all errors, logs them and exits 0; slow work is
  spawned detached. A devkit bug must never block or break an agent session.
- **Re-check before anything destructive.** Plans are recomputed right before acting: live processes, uncommitted and
  unpushed work, a fresh PR state. Removal uses `git worktree remove`, never `rm -rf`.
- **Configure tools through their own interfaces.** Codex is configured through `codex app-server`
  (`config/batchWrite`, `hooks/list`); Claude Code settings are merged key by key; every rewritten file is backed
  up first.
- **Idempotent installs.** `dev install` can run any number of times and only changes what differs.
- **Explain, don't just block.** Every guard denial tells the agent the right way to do the thing.
- **No secrets** in the code, the configuration, or the handoff repository.

## Running the tests

```bash
cd ~/Development/fireflysoftwaresolutions/devkit
python3 -m unittest discover -s tests -t . -v
/usr/bin/python3 -m unittest discover -s tests -t . -v     # Python 3.9 compatibility
python3 -m py_compile lib/devkit/*.py && /usr/bin/python3 -m py_compile lib/devkit/*.py
```

The tests cover guard decisions, remote parsing and project ids for different hosts, `git status` parsing, the CI
workflow reader, the status block parser and format score, transcript reading, work items (locks, versions, reasons,
delegation), plans, progress, and timing, status reports, required skills, the file commands (refusals, the Trash
and undo, capped output), CI/CD status for every host (against recorded API responses), delivery policies, git
hosts and clouds, the diagram engine and the docs diagrams (stale renders and the width budget), and the dashboard's
decisions and escaping. Tests that need state use a
temporary state folder. GitHub CI runs the same suite on Python 3.9 and 3.13; run it locally first.

### Consistency checks

Parts of devkit describe each other: the crew's rules and the functions that enforce them, the parser and
[Commands](commands.md), the registered hooks and [Agents › Hooks](agents.md#hooks), the configuration the code reads
and [Configuration](configuration.md), and the roster and its RACI. `lib/devkit/selfcheck.py` compares them, from
file reads and imports only, in about a second:

| Check | Fails when |
| --- | --- |
| D1 | An enforced rule of `dev crew rules` names a function that does not import, or that no test names |
| D2 | A planned rule names no open work item or plan step (this Mac's state) |
| D3 | A public function of the crew's modules has no caller outside its own definition and the tests |
| D4 | A command or option is missing from [Commands](commands.md) or a top-level command from the `using-devkit` skill's table, or a `dev …` command in the instructions, skills, docs, or roster does not parse |
| D5 | The registered hook events differ from the hooks table or the counts on [Surfaces](surfaces.md) |
| D6 | A configuration key the code reads is missing from [Configuration](configuration.md) |
| D7 | A kind of work breaks RACI hygiene: its approver also consulted or informed, a member both consulted and informed, an approver of its own work without a recorded reason, a knowledge doc that does not exist, or no knowledge skill |
| D8 | A skill the crew names is not installed for its agent (this Mac's state) |
| D9 | A procedure phase's proof, or a command its gate names, is not in the phase's `devkit` list |
| D10 | A kind of work has no file anchor and does not say why, or an open lesson has no anchors (this Mac's state) |
| D11 | A row of the guardrail table in [The crew](crew.md#guardrails) cites no rule id, or states a planned rule as fact |
| D12 | `tests/__init__.py` does not activate `tests/_hermetic.py` (the run's temporary home), or a documented `unittest discover -s tests` lacks `-t .`, which unittest needs to import `tests/__init__.py` |
| D13 | `shell/completions/_dev` or `dev.bash` differs from what the parser generates (`bin/dev completion zsh\|bash`), or a completion rule in `lib/devkit/completion.py` names an argument the parser no longer has |
| D14 | A skill that the crew, a skill rule, the `using-devkit` table, or section 9 of `agents/AGENTS.md` names comes from no source devkit installs, or the crew, a rule, or the table asks a model to load a skill only the developer can run (`user_only` in `config/skills.json`) |

`tests/test_consistency.py` runs the checks that read only the repository, and tests the others on small fixtures.
Drift that exists today is listed in its `KNOWN` allowlist, each item with its review finding, owner, and the task
or plan step that removes it. A new finding fails the test, and so does a listed one that is gone: when you fix one,
take it out of `KNOWN` in the same commit.

```bash
python3 -m unittest tests.test_consistency
python3 -I -c "import sys; sys.path.insert(0,'lib'); from devkit import selfcheck; [print(f) for f in selfcheck.run()]"
```

The second command lists every finding today, including the checks of this Mac's state. Each finding names its
evidence and the fix.

## Trying a change in your own sessions

Your sessions' hooks and the guard run the [pinned copy](getting-started.md#the-pinned-copy) in
`~/.local/share/devkit/releases/`, and so does `dev` in your shell. An edit in the checkout changes nothing for them
until you pin it, so a half-written `guard.py` never switches off the guard of your running sessions. Run the
checkout's own code with `bin/dev` from the checkout.

To use a change in your sessions:

1. Commit it on your branch.
2. Run `bin/dev ci --run` in the checkout. It runs what CI runs and records the evidence for that commit's tree.
3. Run `bin/dev install` yourself, in a terminal (the guard refuses it from an agent). It pins the tested commit and
   re-applies the configuration from the new copy.
4. Start new agent sessions; running sessions keep the copy they started with until they restart.

`dev install --version <previous version>` switches back. `dev install --unverified --why "<reason>"` pins an untested
commit when you must, and records the reason in the audit trail.

## Adding a command

1. Write `cmd_<name>(a)` in `lib/devkit/cli.py`; return an exit code.
2. Register it in `build_parser()` with its options.
3. Add it to `COMMANDS` (grouped help), then regenerate the shell completion from the parser:
   `bin/dev completion zsh > shell/completions/_dev` and `bin/dev completion bash > shell/completions/dev.bash`. When
   an argument takes something devkit knows (a project, a task, a branch), add a rule to `RULES` in
   `lib/devkit/completion.py`. CI fails while the checked-in scripts differ from the parser (D13).
4. Document it in [Commands](commands.md) and, when agents use it, in the `using-devkit` or `workspace` skill.

## Adding a guard rule

1. Add the check to `lib/devkit/guard.py`. Keep it a pure function of the tool input and the file system; return
   `deny(reason)` with a reason that names the right way.
2. Add allow **and** deny cases to `tests/test_devkit.py` (false positives cost as much as misses).
3. Document it in [Guard rules](guard.md) and, if it reflects a working rule, in `agents/AGENTS.md`.

## Change a devkit skill, agent, command, or AGENTS.md

The skills, the crew's agents, the slash commands, and `agents/AGENTS.md` tell every agent how to use devkit. When a
feature changes what agents should do, change them in the same branch, or agents keep doing the old thing.

1. Load the `docs` skill. These files are documentation, written to the same standard, and devkit requires the
   skill for them: skills (`plugin/skills/**`, any `SKILL.md`), agent files, slash commands, and `agents/AGENTS.md`.
2. Change every place that teaches the behavior:

   | When the feature changes | Change |
   | --- | --- |
   | A working rule every session follows | `agents/AGENTS.md`, keeping its sections and their numbers, and its row in [Agents › Global instructions](agents.md#global-instructions) |
   | How a job is done | The skill in `plugin/skills/<name>/SKILL.md` that teaches the job (the `using-devkit` table names it), and the guide in `docs/` |
   | A `dev` command | [Commands](commands.md), the `using-devkit` table for a top-level command, and every skill or doc that shows its old form |
   | A crew member, procedure, or kind of work | `config/crew.json`, then `bin/dev crew sync`, which writes `plugin/agents/*.md` (`dev install` writes the Codex roles from the same roster); never edit a generated agent file. Then [The crew](crew.md) |
   | Which skills a kind of work or a file requires | `config/skill-rules.json` and the rules table in [Skills and plugins › Required skills](skills-and-plugins.md#required-skills) |
   | A slash command | `plugin/commands/<name>.md` with a `description`, under a name no skill uses (Codex gets each command as a skill of the same name), and the slash command list in [Skills and plugins](skills-and-plugins.md) |

3. Run the checks:

   ```bash
   bin/dev crew sync --check                                     # the agent files match config/crew.json
   python3 -m unittest tests.test_consistency tests.test_codex_plugin
   dev docs check
   ```

   The consistency test fails when a `dev …` command in the instructions, skills, docs, or roster does not parse,
   when a top-level command is missing from the `using-devkit` table, when a skill you name comes from no source
   devkit installs or only the developer can run it, and on the other drift in [Consistency checks](#consistency-checks).
4. Try the change in your own sessions (see [Trying a change in your own sessions](#trying-a-change-in-your-own-sessions)):
   your agents read the pinned copy, and `agents/AGENTS.md` reaches `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`
   only through `bin/dev install`.

## Release process

devkit follows its own delivery method: integrate locally, run the full suite, push once.

1. Follow devkit's recorded delivery policy (`dev delivery`): update `VERSION` with the next CalVer version
   (`dev calver --next`) and add a `CHANGELOG.md` entry.
2. Run the tests on both Pythons (see [Running the tests](#running-the-tests)), `dev docs check` and
   `dev diagram check docs/diagrams`.
3. Commit, then run what CI runs (`bin/dev ci --run`). It records the evidence that `dev install` and `dev push`
   check. In your terminal, run `bin/dev install`, which pins this tested commit for your hooks, then `dev doctor`
   and `dev agents test`; every check must pass.
4. Push the release branch once (`dev push`), open one PR (`dev pr create`), and watch its CI
   (`dev ci watch --wait --pr <n>`; an agent arms it as a Monitor or background command and names its task id).
5. Merge with `dev pr merge`, wait for the CI run on `main` (`dev ci watch --wait --pr <n>` follows the merge
   commit), and publish with `dev release`. It tags `vYY.MM.PATCH` on `main` and creates the GitHub release with the CHANGELOG section as its
   notes. The guard refuses a raw `gh pr merge` or `gh release create`.
6. Verify what was published: download the source archive into an empty folder
   (`gh release download vYY.MM.PATCH --archive tar.gz -D <folder>`), compare it with the tag, and run its tests.
7. Developers update with `dev update`, which checks out the new release and pins it.

## Conventions

- Every source file starts with the copyright and license header (`//` comments in JavaScript):

  ```python
  # Copyright (c) 2026 Firefly Software Solutions Inc.
  # SPDX-License-Identifier: Apache-2.0
  ```

- Module docstrings explain purpose and safety rules; user-facing messages are short and actionable.
- Commit messages explain why, in the imperative; no AI attribution.

See also [Guard rules](guard.md), [Agents](agents.md), and [the CHANGELOG](../CHANGELOG.md).
