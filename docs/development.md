# Developing devkit

Maintained by Andrés Contreras Guillén. © 2026 Firefly Software Solutions Inc. All rights reserved.

## Code layout

```text
bin/dev                 entry point (adds lib/ to sys.path, calls devkit.cli.main)
lib/devkit/
  core.py               paths, configuration, shell/git helpers, remote parsing and project ids, process detection
  ui.py                 terminal colours, banner, greeting and tips, tables
  registry.py           project discovery and inspection, PROJECTS.md, registry.json, handoff notes
  guard.py              pre-tool-use workspace rules shared by Claude Code and Codex
  hooks.py              hook entry points: session-start, stop, session-end, guard
  canary.py             instruction canary checks and reports
  ci.py                 GitHub Actions workflow reader for `dev ci`
  gc.py                 safe disk reclamation
  setup.py              `dev install` and `dev doctor`
  codexrpc.py           JSON-RPC client for `codex app-server`
  cli.py                argument parsing and command implementations
agents/AGENTS.md        global instructions rendered for Claude Code and Codex
agents/skills/          devkit's own skills
config/                 Claude Code, Codex, git and skill configuration applied by `dev install`
shell/                  zsh integration and completion
templates/handoff/      initial files of a new handoff repository
tests/                  unit tests
install.sh · Brewfile   bootstrap and toolchain
```

## Design principles

- **Standard library only, Python 3.9 compatible.** `dev` runs with Homebrew Python or the macOS system
  `/usr/bin/python3`, with no virtualenv or dependencies.
- **Hooks fail open and return fast.** Every hook handler catches all errors, logs them and exits 0; slow work is
  spawned detached. A devkit bug must never block or break an agent session.
- **Re-check before anything destructive.** Plans are recomputed right before acting: live processes, uncommitted and
  unpushed work, a fresh PR state. Removal uses `git worktree remove`, never `rm -rf`.
- **Configure tools through their own interfaces.** Codex is configured through `codex app-server`
  (`config/batchWrite`, `hooks/list`); Claude Code settings are merged key by key; every rewritten file is backed
  up first.
- **Idempotent installs.** `dev install` can run any number of times and only changes what differs.
- **Explain, don't just block.** Every guard denial tells the agent the right way to do the thing.
- **No secrets** in the code, the configuration or the handoff repository.

## Running the tests

```bash
cd ~/Development/fireflysoftwaresolutions/devkit
python3 -m unittest discover -s tests -v
/usr/bin/python3 -m unittest discover -s tests -v     # Python 3.9 compatibility
python3 -m py_compile lib/devkit/*.py && /usr/bin/python3 -m py_compile lib/devkit/*.py
```

The tests cover guard decisions, remote parsing and project ids for several hosts, `git status` parsing, the CI
workflow reader, the canary detector and transcript reader, and handoff-note session lines. Tests that need state
use a temporary state folder.

## Adding a command

1. Write `cmd_<name>(a)` in `lib/devkit/cli.py`; return an exit code.
2. Register it in `build_parser()` with its options.
3. Add it to `COMMANDS` (grouped help) and to `shell/completions/_dev`.
4. Document it in [Commands](commands.md) and, if relevant, in the devkit `workspace` skill.

## Adding a guard rule

1. Add the check to `lib/devkit/guard.py`. Keep it a pure function of the tool input and the file system; return
   `deny(reason)` with a reason that names the right way.
2. Add allow **and** deny cases to `tests/test_devkit.py` (false positives cost as much as misses).
3. Document it in [Guard rules](guard.md) and, if it reflects a working rule, in `agents/AGENTS.md`.

## Release process

1. Update `VERSION` (semantic versioning) and add a `CHANGELOG.md` entry.
2. Run the tests on both Pythons (above).
3. `dev install` on your machine and `dev doctor` — every check must pass.
4. Commit, tag and publish:

   ```bash
   git commit -am "Release vX.Y.Z"
   git tag -a vX.Y.Z -m "devkit vX.Y.Z"
   git push && git push --tags
   gh release create vX.Y.Z --title "devkit vX.Y.Z" --notes-file <(sed -n '/^## \[X.Y.Z\]/,/^## \[/p' CHANGELOG.md | sed '$d')
   ```

5. Developers update with `dev update`.

## Conventions

- Every source file starts with the copyright header:

  ```python
  # Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
  ```

- Module docstrings explain purpose and safety rules; user-facing messages are short and actionable.
- Commit messages explain why, in the imperative; no AI attribution.

See also: [Guard rules](guard.md) · [Agents](agents.md) · [the CHANGELOG](../CHANGELOG.md)
