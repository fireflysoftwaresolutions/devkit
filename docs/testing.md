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

`dev test` finds the project's test runner (pytest, unittest, Jest, Vitest, Node's `node --test`, Playwright, the
Angular CLI's `ng test`, Maven, Gradle, Go, PHPUnit, or Pest), runs the selection with it, and prints a compact result:
each failure with `file:line` and its first error line, and the path to the full log. The raw output streams only to a
terminal, or with `-v`. A package gets the Node runner when its `package.json` test script runs `node --test`, or runs
its test scripts with `node`, and `node` is on the `PATH`. A path you name must be inside the repository:
`dev test run ../other/a.test.js`, an absolute path outside it, or a symlink that leads out of it is refused with
`no test runner for:` and runs nothing. A list passed as one word, which zsh does with an unquoted `$MODULES`, is split
into its tests, and the run says so; write `${=MODULES}` in zsh to pass the words yourself.

The command comes from the project first. Each runner takes the first plain call of the same runner in the same folder
that the project declares: a `test_gate` entry of its delivery policy (`dev delivery`), then a step of its CI
workflows (as `dev ci` reads them), then the `package.json` test script. A bare `python` in a CI step becomes the
interpreter devkit found here. Only without a declaration does devkit infer the command from the project's files.
`dev test plan` prints which (`command: declared by …` or `inferred`), and `dev test --json` gives `source` and
`declared_by` for every run.

With `--listed` (or `"test": {"listing": true}` in devkit's configuration), the runner's own listing decides which
files hold tests: `pytest --collect-only`, unittest's discovery, `vitest list`, `playwright test --list`, or
`jest --listTests`. A file named like a test that the runner does not collect is no test, and a file it collects under
another name is one. The listing is cached in devkit's state and runs again only when the runner's configuration
files or the names of its files change. devkit's own suite lists in about 4.5 seconds, then 1 second from the cache;
a large pytest suite can take 30 seconds, which is why it is not the default yet.

Some test files belong to a more specific runner than their package's Jest or Vitest:

- **Browser specs.** The specs in the `testDir` folders of a `playwright.config.*` run with Playwright
  (`npx playwright test <files>`); Vitest and Jest leave them out. A config without `testDir`, or whose `testDir` is
  its own folder (`'.'`), gives Playwright the specs of its folder that import `@playwright/test`, directly or
  through a local fixtures file, and the unit tests beside them keep their runner. A type imported from
  `@playwright/test` (`import type`, `import { type Page }`, `export type … from`) loads nothing, so a unit test that
  only reaches such a type stays with its runner. A commented-out `testDir` counts as none.
- **Angular unit tests.** A project in `angular.json` whose `test` target uses the unit-test builder
  (`@angular/build:unit-test`) with its Vitest runner runs its specs through `ng test <project> --include <file>`, so
  they build with the workspace's TypeScript paths, setup files, and providers, which a bare Vitest run lacks. Its specs
  are the files its `include` patterns match (by default `**/*.spec.ts` and `**/*.test.ts` under its source root); a
  test file under the same folder that no pattern matches keeps the package's runner.

pytest's own configuration decides which Python files are tests. A file named like a test (`test_*.py`, `*_test.py`)
is one only inside the `testpaths` of the nearest pytest configuration (`pytest.ini`, `pyproject.toml`, `tox.ini`,
`setup.cfg`) and when its name matches that configuration's `python_files`. A production module such as
`app/test_probe.py` outside them is source code: a change to it selects the tests that import it, and
`dev test prove` undoes it as part of your change. A Python project nested inside another, with its own
`pyproject.toml` and tests (a worker package), has its own runner: the outer project's full pytest run leaves its
folder out, so the two suites never share one process.

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
| A file that a code file names by its whole path (`'lib/devkit/state.py'` in a script) | the tests of that code file |
| Any file under a folder that a test walks (`(ROOT / "lib" / "devkit").rglob("*.py")`, `os.walk("pkg")`) | that test, though it imports nothing |
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

### Interrupted runs are incomplete

For unittest, devkit observes the outer test invocation and records its completion separately from the output.
A missing, invalid, or stopped result is incomplete, even if the process exits with code 0. A nested runner's
summary cannot complete the outer selection; buffered output after the outer summary does not invalidate it.

An incomplete run reports a runner failure and keeps unresolved failures for tests that never finished. Do not
treat the assertions printed before an interruption as the complete result, or an inner "Ran 0 tests" summary
as proof that the selected tests disappeared.

The other runners (pytest, Jest, Vitest, Go, Maven, Gradle, PHPUnit, Node) write no such record. Their run is
complete when it exits with `0`, or with `1` and the failures it reports; each failure counts once. A run that stops
at its first failure (`-x`, `--bail`) is incomplete. A run that exits with an error and names no failed test is a
runner failure.

### The record of failures

devkit keeps one record of test results per project, shared by every worktree and agent lane, and writes it under a
lock, so two runs at the same time both keep their results. Each run writes its own log, in a folder per worktree;
the newest 10 logs per runner stay.

A failure is recorded with its module or file, so the next `dev test` reruns that exact test. When a completed rerun
confirms that a recorded test no longer exists (renamed, or its file deleted), it names the test and removes it
from the record:

```text
! 1 recorded failure(s) no longer select a test (renamed or deleted): forgotten: tests.test_canary.AliasTests.test_old_name
```

A runner's own answer for a name that is gone counts as that confirmation, whatever its exit code: pytest stops with
"no tests ran" and exits with `4`. An incomplete unittest run cannot remove failures this way. A test module that
exists but fails to import is a real failure and stays recorded. A failure recorded in a file that is no test here any
more (a module named `test_*.py` outside pytest's `testpaths`, which an older devkit ran) is named and forgotten, so it
never blocks `dev test` again.

### After a CI step fails

```bash
dev ci --run              # what CI runs, locally, with evidence for dev push
dev ci --run --resume     # after a fix: only the failed steps and what the fix affects
```

`--resume` reruns the failed steps and the steps that never ran. A step that passed earlier runs again only when the
files changed since then can affect it. A test step with valid completion evidence reruns only its failed tests
plus the affected ones. An interrupted selection, or an older record without completion evidence, reruns the whole
test step. This also applies when a previous partial rerun was interrupted. Other unaffected steps can stay kept.
Full unittest steps retain the original shell's variable expansion, quoting, working directory, and interpreter.
A partial rerun past the step's time limit stops every process it started (the runner, its workers, and the tests'
own children), as a full step does.

devkit reads the failed tests of a step only when it recognizes the step's test command. A multi-line step counts
when exactly one line runs a test command and every other line cannot change what that command runs: blank lines,
comments, assignments, `export`, `echo`, `printf`, `cd`, and `set -e`, `-u`, `-x` or `-o pipefail`. The test command
runs in the folder the `cd` lines lead to, with the variables the step exports. A step with two test commands, a
loop, a conditional, a function, a here-document, a command substitution, or any other command reruns whole, and
devkit says which line made it so.

When it all passes, devkit records evidence of the kind `resumed`, which `dev push` and `dev pr create` accept and the PR
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
   as part of the change and is restored too, and so is changed helper code inside a Python test module, outside its
   tests, so a fix that lives only in a helper is proven. Such a helper comes back whole, without the lines the change
   added inside it; a helper the change adds stays. When one diff hunk both edits a helper that was there and adds
   another, the hunk is undone line by line: the added helper's lines stay and the edited lines go back. If that
   cannot be done cleanly, the module's tests are "cannot be proven." A test that still passes is reported as "does
   not test the change."
2. **It is stable.** It runs the test five times with your change (`--runs N` changes the count). A test that is not
   five times green is reported as flaky. This is the rule Meta's TestGen-LLM applies to generated tests.

A test counts as new or changed when its own text differs from the same-named test at the base, so a test that moved,
or that new tests were inserted around, stays unchanged. A changed helper of the test file (a method of a shared test
class, a module function, a JavaScript function between two `it()` blocks) changes the tests that call it, directly or
through other helpers; `setUp`, a `beforeEach`, a module setup, or an autouse fixture changes every test it runs for.
A helper the change adds changes only the tests that call it, such as an unchanged test that called a base class's
method of that name. A test file whose only change is a helper that no test calls has no changed tests, so `prove`
leaves it out (`no_changed_tests` in `--json`). A change outside every test and helper, such as an import, still
counts every test of the file.
A JavaScript test ends at the bracket that closes its `it()` or `test()` call, and `it.each` and `test.each` are one
test named by their title template (`'adds %i and one'`, `'returns $b for $a'`), which matches each of their cases.

The run without your change records which files each Python process ran, as it runs them: an audit hook sees every
module's code, so an import inside `patch.dict(sys.modules)` counts too, and files are compared by identity, so a path
in another letter case is still the checkout. `dev test prove` says "proves the change" only with that record:

- A changed file imported from the checkout, where your change stays (a `.pth` file that puts the checkout's folder
  first, or a built copy of the checkout's package that the copy cannot replace), makes its tests "cannot be proven:
  imported the checkout's `<file>`."
- A run that left no record, or that started a Python child that skips the record (`-I`, `-E` or `-S`, or an
  environment whose `PYTHONPATH` leaves out devkit's recorder), is "cannot be proven: no record of what the run
  imported," and the output names the child's command. The recorder finds such a child behind `/usr/bin/env` (with
  its options, `-i` and `NAME=value` words), behind leading `NAME=value` words in a shell line, inside `sh -c`,
  `bash -lc`, `sh -ec` or `bash -c --` (also after `--rcfile` or `--init-file` and its file), behind a program that
  runs the rest of its words (`timeout 30 python`, `nice -n 5 python`, `xargs python`), also when that program starts
  a shell or `env` first (`timeout 30 bash -c "python -I …"`, `xargs sh -c …`, `timeout 30 env -i python`), through
  `subprocess`'s `executable=`, and after the value of `-W`, `-X` or `--check-hash-based-pycs`. The words of `echo`
  and `printf` start nothing, so a Python word among them is not flagged. When the proof spans more than one
  test file, each Python test file runs once more with a record of its own, so only the files that start such a child
  lose their proof; a file that does not load on that run (state the first run left stops it) has no record.
- A failing test whose run imported none of the changed Python files from the copy, and read none of the changed data
  files there, is "cannot be proven: imported none of the changed files from the copy": nothing shows that it ran the
  code without your change. An open that empties a data file first (`w+`, `O_TRUNC`) reads none of its content.

The record has limits. A Python child that a program other than Python starts is recorded when it keeps the
recorder, since it inherits `PYTHONPATH`; one that skips it there goes unseen (a shebang script with `-I`, a
`"$PY" -I` shell line, `make`, `tox`, or devkit's own `bin/dev`). The record also misses a module run from a
sourceless `.pyc` compiled under another filename, a plain copy of the source inside the checkout (`build/lib`), and
source that a test reads from the checkout and runs itself (`exec(compile(path.read_text(), …))`), which is a read of
a `.py` file, not an import. Such a test may show "does not
test the change" when it does; prove it with a test that imports the module directly.

For JavaScript, the only evidence checked is a workspace package that the linked `node_modules` resolves into the
checkout, which also makes its tests "cannot be proven: imported the checkout's `<file>`." A proven JavaScript test
says so (`imports_checked: workspace links only` in the JSON, a note in the output). `dev test prove --json`
lists the evidence under `imports`: `from_copy` and `from_checkout`, and the unrecorded children under `unrecorded`.

The copy carries a clone of the checkout's git repository that shares its objects, so a test that runs git on its
checkout works there. The dependency folders (`node_modules`, `.venv`, `vendor`) are linked into the copy, not
copied. A Python project installed in editable mode with a `src` layout would import the checkout's code, with your
change, through the linked environment's `.pth` file, so devkit puts the copy's own source folders that those files
name first on `PYTHONPATH`, and the run without the change really runs the old code.

A project installed as a built copy instead (`uv sync --no-editable`, `pip install .`) leaves no such file:
site-packages holds the build of your changed code. devkit finds the install by its `direct_url.json`, which names
the checkout, and puts the copy's folder that holds the same files first, so the old code runs there too. When the copy
has no such folder (the package is built from a folder of another name), `dev test prove` names the package and says
that its tests cannot be proven this way: install it in editable mode (`uv sync`, `pip install -e .`), or put its
source folder on `PYTHONPATH`.

The copy lives in devkit's scratch folder (`~/Development/.tmp/devkit-prove-<process id>-…`) and is removed when the
run ends. A run that crashed or was killed leaves its copy behind: the next `dev test prove` removes the copies older
than a day whose process is gone, and says how many. `dev du` names them, with their size, on its devkit scratch
files row.

A test that its runner skips in every run with your change (a `skipif`, a missing dependency) ran neither way: it is
listed as skipped, with a warning, as neither proven nor failing. The command exits `0` when every test that ran proves
the change, `1` when one does not, and `2` when none could be proven because each was skipped.

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

### Evidence for lesson prevention

`dev test prove --isolated-imports [test files] --runs N [--since REF] [--json]` can produce prevention evidence
only for direct unittest checks under the current interpreter. It runs with `-I -S`, removes inherited
`PYTHON*` settings, and uses checkout-local roots and the canonical standard library. Editable installs,
site packages, `.pth`, customization hooks, and inherited `PYTHONPATH` or home imports are excluded.

Observed external reads, subprocesses, native loads, unresolved inputs, or exhausted provenance budgets make
suppression ineligible. The causal test result remains available. Ordinary `prove` also keeps its causal result
but writes no suppression receipt. Check `prevention_eligible` in JSON or the unverified reason in text.

Private schema-3 evidence binds the exact check, scope, runtime, tool, and bounded checkout snapshot to
`isolated-unittest-v1`. The snapshot covers the files the proof copy holds: tracked and untracked files, not the
ones git ignores. Ignored build output such as `dist/` is not part of the evidence and does not void it; a change to
any other file does. It is fresh for at most 30 days and is revalidated before recall is suppressed.
This is evidence of observed origins, not a comprehensive attestation of arbitrary external data. See
[Lessons](lessons.md#recur-and-prevent).

## TDD with devkit

1. **Red, for the right reason.** Write the test and run it (`dev test run <file>`). It must fail on the missing
   behavior. A failure from an import error or a typo does not count.
2. **Green, with the simplest code.** `dev test` reruns the failure first.
3. **Refactor,** then `dev test` again for what the refactor can break.

When the tests come after the code, for a bug fix or legacy work, `dev test prove` replaces the red step.

## Unfinished work

devkit builds things for real. `dev stubs` finds TODO and FIXME markers, "not implemented" code, placeholders, and
empty Python bodies in your changes; the Stop hook sends a turn back for them, and a commit that would take them gets
a warning, unless the line names its task (`TODO(T-…)`) or issue. See [Commands › Quality](commands.md#quality).

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
