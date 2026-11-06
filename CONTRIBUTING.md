# Contributing to devkit

Thank you for helping. This guide covers how to propose a change, the checks a pull request must pass, and the
conventions devkit follows. The code layout and the design principles are in
[Developing devkit](docs/development.md).

## Before you start

- **A bug or an idea:** open an issue. For a bug, say what you expected, what happened, the devkit version
  (`dev version`), your macOS version, and the agent (Claude Code, Codex, Claude Desktop, or the ChatGPT app).
- **A security issue:** never in an issue. Follow the [security policy](SECURITY.md).
- **A larger change** (a new command, a new hook, a change to the guard or the installer): open an issue first, so
  you and the maintainers agree on the approach before you write the code.

## Work on a clone

devkit is standard-library Python and runs from its folder, so you can change it and run its tests without
installing it:

```bash
git clone https://github.com/fireflysoftwaresolutions/devkit.git
cd devkit
python3 -m unittest discover -s tests
/usr/bin/python3 -m unittest discover -s tests   # Python 3.9, the macOS system Python
bin/dev --help
```

Installing devkit (`./install.sh`) changes your agents' global settings, hooks, and instructions under `~/.claude`
and `~/.codex`. Do it only when you want devkit to manage your machine; see [Getting started](docs/getting-started.md).

## Make the change

1. Branch from an up-to-date `main`: `<type>/<topic>`, for example `fix/guard-quoted-paths`. Types are `feat`, `fix`,
   `docs`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, and `deps`; the topic is lowercase kebab-case, and
   the whole name is at most 60 characters.
2. Write the test first, and see it fail for the reason you expect. A test must be able to fail for a real bug:
   `bin/dev test prove` checks that new tests fail without your change.
3. Keep the design principles: the standard library only, Python 3.9 compatible, hooks that fail open, and a guard
   refusal that always names the right way to do the thing.
4. Update the documentation in the same branch, with a command in [Commands](docs/commands.md), a guard rule in
   [Guard rules](docs/guard.md), and anything a user notices in the `[Unreleased]` section of the
   [CHANGELOG](CHANGELOG.md).
5. To change a crew member, edit `config/crew.json` and run `bin/dev crew sync`. The files in `plugin/agents/` are
   generated: never edit them by hand.

## Run the checks

Every check must pass before you open the pull request. CI runs the tests on Python 3.9 and 3.13 and a security job.

| Check | Command |
| --- | --- |
| The tests, on both Pythons | `python3 -m unittest discover -s tests` and `/usr/bin/python3 -m unittest discover -s tests` |
| The code compiles | `python3 -m compileall -q lib bin` |
| No test that cannot fail | `bin/dev test audit tests/` |
| No unfinished work (stubs, placeholders, TODOs without a task) | `bin/dev stubs` |
| The documentation | `bin/dev docs check` (markdownlint, lychee, and Vale) |
| Everything CI runs, locally | `bin/dev ci --run` (the security job needs Docker) |

## Commits and pull requests

- Write commit messages as Conventional Commits: `type(scope): summary`, in the imperative, at most 72 characters,
  with a body that says why. Make one logical change per commit.
- Do not add AI attribution, such as `Co-Authored-By` lines for tools or "Generated with" lines.
- Fill in every section of the pull request template (what and why, changes, how to verify, risk and rollback, and
  links).
- Keep one change per pull request, rebased on `main`. A maintainer merges it after CI passes and a review.

## License

devkit is licensed under the [Apache License 2.0](LICENSE). By submitting a contribution, you agree that it is
licensed under the same terms (section 5 of the license) and that you have the right to submit it.

See also [Developing devkit](docs/development.md), [Commands](docs/commands.md), and the
[security policy](SECURITY.md).
