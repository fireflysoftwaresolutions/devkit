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
  providers.py          Claude Code and Codex status, quota and self-tests
  setup.py              dev install and dev doctor
  wizard.py             dev setup
  toolchain.py          git hosts and clouds: their CLIs, sign-in checks and Homebrew groups
  codexrpc.py           JSON-RPC client for codex app-server
agents/AGENTS.md        global instructions rendered for Claude Code and Codex
plugin/                 the devkit plugin: skills, slash commands, the scout agent, icon
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
python3 -m unittest discover -s tests -v
/usr/bin/python3 -m unittest discover -s tests -v     # Python 3.9 compatibility
python3 -m py_compile lib/devkit/*.py && /usr/bin/python3 -m py_compile lib/devkit/*.py
```

The tests cover guard decisions, remote parsing and project ids for different hosts, `git status` parsing, the CI
workflow reader, the status block parser and format score, transcript reading, work items (locks, versions, reasons,
delegation), plans, progress, and timing, status reports, required skills, the file commands (refusals, the Trash
and undo, capped output), CI/CD status for every host (against recorded API responses), delivery policies, git
hosts and clouds, the diagram engine and the docs diagrams (stale renders and the width budget), and the dashboard's
decisions and escaping. Tests that need state use a
temporary state folder. GitHub CI runs the same suite on Python 3.9 and 3.13; run it locally first.

## Adding a command

1. Write `cmd_<name>(a)` in `lib/devkit/cli.py`; return an exit code.
2. Register it in `build_parser()` with its options.
3. Add it to `COMMANDS` (grouped help) and to `shell/completions/_dev`.
4. Document it in [Commands](commands.md) and, when agents use it, in the `using-devkit` or `workspace` skill.

## Adding a guard rule

1. Add the check to `lib/devkit/guard.py`. Keep it a pure function of the tool input and the file system; return
   `deny(reason)` with a reason that names the right way.
2. Add allow **and** deny cases to `tests/test_devkit.py` (false positives cost as much as misses).
3. Document it in [Guard rules](guard.md) and, if it reflects a working rule, in `agents/AGENTS.md`.

## Release process

devkit follows its own delivery method: integrate locally, run the full suite, push once.

1. Follow devkit's recorded delivery policy (`dev delivery`): update `VERSION` with the next CalVer version
   (`dev calver --next`) and add a `CHANGELOG.md` entry.
2. Run the tests on both Pythons (see [Running the tests](#running-the-tests)), `dev docs check` and
   `dev diagram check docs/diagrams`.
3. Run `dev install` on your machine, then `dev doctor` and `dev agents test`; every check must pass.
4. Run what CI runs (`dev ci --run`), then push the release branch once (`dev push`), open one PR
   (`dev pr create`), and watch its CI (`dev pr checks --watch`).
5. Merge with `dev pr merge`, wait for the CI run on `main` (`dev ci status --watch`), and publish with
   `dev release`. It tags `vYY.MM.PATCH` on `main` and creates the GitHub release with the CHANGELOG section as its
   notes. The guard refuses a raw `gh pr merge` or `gh release create`.
6. Verify what was published: download the source archive into an empty folder
   (`gh release download vYY.MM.PATCH --archive tar.gz -D <folder>`), compare it with the tag, and run its tests.
7. Developers update with `dev update`.

## Conventions

- Every source file starts with the copyright and license header (`//` comments in JavaScript):

  ```python
  # Copyright (c) 2026 Firefly Software Solutions Inc.
  # SPDX-License-Identifier: Apache-2.0
  ```

- Module docstrings explain purpose and safety rules; user-facing messages are short and actionable.
- Commit messages explain why, in the imperative; no AI attribution.

See also [Guard rules](guard.md), [Agents](agents.md), and [the CHANGELOG](../CHANGELOG.md).
