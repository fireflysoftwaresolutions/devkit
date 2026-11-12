# Testing

devkit holds every test to two rules: a test is worth writing only if it can fail for a real bug in the code it
covers, and worth running only if the change can break it. This guide shows the commands that apply those rules, the
checks that enforce them, and how agents are told to work. The `testing` skill carries the same method for Claude Code
and Codex.

## Run only what a change can break

```bash
dev test                  # the tests that failed last time first; if they pass, the tests your changes affect
dev test plan             # what would run and why (changed file → tests), with the time estimate; runs nothing
dev test run --failed     # only the last failures
dev test run --affected   # only the tests your changes affect
dev test run --all        # the full suite
dev test run tests/test_parser.py   # these tests
```

`dev test` finds the project's test runner (pytest, unittest, Jest, Vitest, Node's `node --test`, Maven, Gradle, Go,
PHPUnit, or Pest), runs the selection with it, and prints a compact result: each failure with `file:line` and its first
error line, and the path to the full log. The raw output streams only to a terminal, or with `-v`. A package gets the
Node runner when its `package.json` test script runs `node --test`, or runs its test scripts with `node`, and `node`
is on the `PATH`. A path you name must be inside the repository: `dev test run ../other/a.test.js`, an absolute path
outside it, or a symlink that leads out of it is refused with `no test runner for:` and runs nothing.

devkit runs `node` itself, not `npm test`, so it gives no runner to a script it cannot reproduce exactly: one that
builds before its tests (a `pretest` script, `tsc && node --test dist/test/`, `npm run build && node --test`), which
would leave the tests to run on stale output, one that passes a Node option devkit does not know without `=`
(`--some-option value`), whose value could be the next word, and one that pipes or redirects its tests
(`node --test | tee out.txt`, `node --test > out.txt`, `node --test < input.txt`). Such a package's tests are never
run wrongly: `dev test` names them in its `no runner here for:` warning, and `dev test prove` lists them as not proven
with the reason. To get the runner back, test the sources without a build step (Node runs `.ts` test files directly
through its type stripping, or through a loader: `node --import tsx --test test/`), write an unusual option with `=`
(`--some-option=value`), and drop the pipe or redirect (devkit keeps the full log of every run); otherwise run
`npm test` yourself. A step after the tests (`node --test && eslint .`) changes nothing.

### The blast radius

devkit maps the files you changed to the tests that can see them:

| Changed file | Tests selected |
| --- | --- |
| A test file | that test file |
| A test helper | the tests that use it |
| A source file | the tests that import it, directly or through other modules (Python imports, JavaScript and TypeScript imports, Java and Kotlin class references, Go packages, PHP `use` statements), the tests named after it (`parser.py` and `test_parser.py`), or Jest's and Vitest's own related-test search |
| A data, template, or configuration file that code reads by name | the tests of the code that reads it |
| Lint, format, or release tool configuration | nothing |
| Documentation | nothing, unless code reads the file by name |
| A file no test maps to | the tests of its directory or package, widened up to all tests, never nothing |
| Build files, lockfiles, test configuration, shared fixtures, CI files | everything: they can break any test |

`dev test plan` prints the reason for each changed file, for example `read by lib/devkit/setup.py` or
`CI file: every test can depend on it`.

When the radius is everything, devkit runs the part it selected, names the file that caused it, and gives the time a
full run takes, instead of starting hours of tests on its own. Run the full suite once, before the push: `dev ci --run`.

A branch far from its base, such as a long-lived integration branch, reaches much of the suite the same way. When the
branch differs from its base in 100 or more files and they reach 50 or more test files, plain `dev test` does not start
them. It says how many test files and how long they take, names the tests of the files you touched now (your
uncommitted changes), and exits with code `2`:

```text
! not started: this branch differs from origin/main in 1,388 file(s), which reach 205 test file(s) (~111m00s)
  → the tests of the files you touched now: dev test run tests/test_parser.py
  → or compare with the commit your work started from: dev test --since <ref> · all 205 of them: dev test --affected
```

Run the command it names, compare with the commit your work started from (`dev test --since <ref>`), or run the whole
selection with `dev test --affected`. Only plain `dev test` holds back: `dev test run`, with or without paths, starts
what it selects.

Mapping is fast: under half a second on repositories of a few thousand files.

### Zero tests is never a pass

A selection that runs no test (a mistyped name, a filter that matches nothing) exits with code 2 and says which
selector and which changed files were involved. A runner's own "no tests found" is not accepted as success.

### The record of failures

devkit keeps one record of test results per project, shared by every worktree and agent lane, and writes it under a
lock, so two runs at the same time both keep their results. Each run writes its own log, in a folder per worktree;
the newest 10 logs per runner stay.

A failure is recorded with its module or file, so the next `dev test` reruns that exact test. When a recorded test
no longer exists (renamed, or its file deleted), the rerun says so, names it, and removes it from the record:

```text
! 1 recorded failure(s) no longer select a test (renamed or deleted): forgotten: tests.test_canary.AliasTests.test_old_name
```

A test module that exists but fails to import is a real failure and stays recorded.

### After a CI step fails

```bash
dev ci --run              # what CI runs, locally, with evidence for dev push
dev ci --run --resume     # after a fix: only the failed steps and what the fix affects
```

`--resume` reruns the failed steps and the steps that never ran. A step that passed earlier runs again only when the
files changed since then can affect it, and a test step reruns only its failed tests plus the affected ones. When it
all passes, devkit records evidence of the kind `resumed`, which `dev push` and `dev pr create` accept and the PR
describes honestly, for example "full on abc123 + 37 affected tests on def456." CI still runs everything.

## Tests that can fail

```bash
dev test audit --changed          # the test files you changed
dev test audit tests/             # a folder, or the whole suite
dev test prove                    # new or changed tests: do they fail without your change?
```

### `dev test audit`

It reads the tests of every stack and reports what makes a test worthless:

| Severity | Rule | What it means |
| --- | --- | --- |
| high | `no-assertion` | The test checks nothing |
| high | `trivial-assertion` | `assert True`, the same expression on both sides, a literal only |
| high | `empty-test` | The body is empty |
| high | `swallowed-failure` | A catch-all `try` hides the failure |
| medium | `skipped-without-reason` | A skip with no reason |
| medium | `sleep-in-test` | A fixed wait instead of a condition |
| medium | `duplicate-test` | Two tests with the same body |
| medium | `mock-only` | Every assertion is about mocks set up in the same test |
| medium | `snapshot-only` | The only assertion is a snapshot |
| medium | `no-production-reference` | The file never touches the project's code |
| medium | `smoke-only` | The test only checks that the code does not crash |
| low | `debug-output`, `vague-name` | Prints in tests, names such as `test1` |

Each finding comes with the fix. Low findings are hidden unless you pass `--min-severity low`, and the command exits
with `1` when a high finding remains. Precision comes first: a good test that is flagged is worse than a missed one, so
helper assertions, `pytest.raises`, table-driven Go tests, and AssertJ chains count as assertions.

The Stop hook runs the audit on the test files a development turn changed and sends the turn back when a test cannot
fail. Fix the test or delete it; adding a meaningless assertion is not a fix.

### `dev test prove`

For each new or changed test, `prove` checks two things in a temporary copy of the repository (your work tree is
never touched):

1. **It fails without your change.** devkit restores the base version of every production file you changed (the
   merge-base with `origin/<default>`, or `--since REF`) and runs the test. A changed test helper or fixture counts
   as part of the change and is restored too, so a fix that lives only in a helper is proven. A test that still
   passes is reported as "does not test the change."
2. **It is stable.** It runs the test five times with your change (`--runs N` changes the count). A test that is not
   five times green is reported as flaky. This is the rule Meta's TestGen-LLM applies to generated tests.

The copy carries a clone of the checkout's git repository that shares its objects, so a test that runs git on its
checkout works there. The dependency folders (`node_modules`, `.venv`, `vendor`) are linked into the copy, not
copied. A Python project installed in editable mode with a `src` layout would import the checkout's code, with your
change, through the linked environment's `.pth` file, so devkit puts the copy's own source folders that those files
name first on `PYTHONPATH`, and the run without the change really runs the old code. The command exits with `1` unless
every test proves the change.

- **Each case on its own.** A parametrized test is reported case by case, by its id (`test_label[None]`,
  `test_label[a]`), each with its own result with and without the change.
- **Node tests.** Tests that `node --test` runs are proven with Node: a `node:test` case by its title, and a plain test
  script (one that exits non-zero when a check fails) as one test. In a `test` folder, a script that another file of
  that folder imports or names in quotes is a helper (a harness, a fake), so it counts as part of the change. Name it
  to prove it as a test: `dev test prove test/protocol.mjs`.
- **What cannot run here is listed, not fatal.** A changed test that no runner here can run (no runner for its
  language, `node` missing, or left out by `--runner`) and a named file that is no test are listed under "not proven"
  with the reason; the other tests are proven. When nothing is left to prove, the command exits with `2` and lists
  them.

## TDD with devkit

1. **Red, for the right reason.** Write the test and run it (`dev test run <file>`). It must fail on the missing
   behavior. A failure from an import error or a typo does not count.
2. **Green, with the simplest code.** `dev test` reruns the failure first.
3. **Refactor,** then `dev test` again for what the refactor can break.

When the tests come after the code, for a bug fix or legacy work, `dev test prove` replaces the red step.

## Unfinished work

devkit builds things for real. `dev stubs` finds TODO and FIXME markers, "not implemented" code, placeholders, and
empty Python bodies in your changes, and the Stop hook and the commit guard refuse them unless the line names its task
(`TODO(T-…)`) or issue. See [Commands › Quality](commands.md#quality).

## Configuration and data

| What | Where |
| --- | --- |
| Last failures, durations per test file, last full-run time | `~/.local/state/devkit/tests/<project>.json` |
| Logs of each run | the path `dev test` prints |
| Runner choice when a repository has more than one | `dev test run --runner pytest` |

## See also

- The `testing` skill: the strategy per kind of code, flaky tests, mutation testing, and diagnosing an existing suite.
- [CI/CD pipelines](ci-cd.md): what CI runs and how failures are diagnosed.
- [Delivery](delivery.md): local CI evidence and the single push.
