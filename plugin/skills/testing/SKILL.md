---
name: testing
description: "Tests that pay for themselves—a test strategy per kind of code, tests that can fail for a real bug (and how devkit checks it with `dev test audit` and `dev test prove`), TDD done right, and running only what a change can break (`dev test`: failed first, then the blast radius; the full suite once before the push). Also flaky tests, mutation testing and diff coverage, and diagnosing a slow or noisy suite in an old or external project. Use whenever you write, change, review, run or debug tests, plan a test strategy, or a suite is slow, flaky or failing."
---

# Testing

A test is worth writing only if it can fail for a real bug in the code it covers, and worth running only if the
change can break it. Everything below follows from those two rules.

## 1. Choose the right kind of test

| Code | Test it with | Notes |
| --- | --- | --- |
| Domain rules, calculations, parsing, state machines | Unit tests, many small ones | No mocks of your own code; property-based tests for wide input domains (`property-based-testing` skill) |
| Database, queues, HTTP clients, file formats | Integration tests against the real thing | Testcontainers for Postgres, MySQL, Kafka, Redis; no in-memory stand-ins for the production database |
| A public API (HTTP, events, library surface) | Contract tests | From the OpenAPI or schema; one per operation and error shape |
| UI components | Component tests on behavior (what the user sees and does) | A few end-to-end flows for the critical paths only (`playwright-cli`) |
| A CLI | Golden output for the main commands, exit codes for errors | |
| Framework wiring, generated code, trivial getters | Nothing | The framework's own tests cover it |

Mock only at the boundary you do not own (a payment provider, the clock, randomness). A test that mocks the code under
test, or asserts what a mock was told to return, proves nothing.

## 2. Write tests that can fail

- **One behavior per test**, named for that behavior: `rejects_an_expired_card`, not `test1` or `it works`.
- **Assert the outcome:** the returned value, the stored state, the error and its message, the event emitted. Not
  literals (`assert True`), not the same expression on both sides, not only that a mock was called.
- **Do not re-derive the expected value** with the same formula as the code. Use a known example worked out by hand.
- **Cover the edges** that break code: empty, one, many; boundaries (0, -1, max); invalid input; unicode; time zones and
  DST; duplicates; concurrency where it exists; and every error path the caller can see.
- **Deterministic:** a fixed clock, seeded randomness, no `sleep` (wait for a condition), no order dependence, no real
  network outside integration tests.
- **Fast:** a unit test runs in milliseconds. Put slow tests in an integration group that CI runs on its own job.
- **Fewer, sharper tests** beat many shallow ones. Do not add a test that duplicates one that already exists, and do not
  pad a change with tests that only execute lines for coverage.

devkit checks this mechanically:

```bash
dev test audit --changed          # tests that cannot fail: no assertion, trivial or swallowed assertions,
                                  # empty bodies; plus sleeps, skips without a reason, mock-only, duplicates
dev test prove                    # each new or changed test must FAIL with your production change removed
```

The Stop hook runs the audit on the test files a turn changed and sends the turn back when a test cannot fail. Fix the
test or delete it; never weaken the detector's finding by adding a meaningless assertion. A crew member's changed
tests get the same audit when it stops (the crew gates).

One known gap of `dev test prove`, until it is fixed:

- **It runs the tests in a plain copy of the repository, without `.git`.** A test that clones its own repository or
  runs git on the repository root fails there. Build test repositories from files: copy what the test needs into a
  temporary folder, `git init`, and commit.

## 3. TDD done right

1. **Red, for the right reason.** Write the test, run it, and read the failure: it must fail because the behavior is
   missing (an assertion with the expected value), not because of an import error, a typo, or a missing fixture.
2. **Green, with the simplest code** that passes.
3. **Refactor** with the test green, then run the affected tests again (`dev test`).

When tests come after the code (a bug fix, a legacy change), `dev test prove` replaces the red step: it runs the new
tests against the code without your change and reports any test that still passes ("does not test your change").

For a bug: first a test that reproduces it and fails, then the fix (the `systematic-debugging` skill).

## 4. Run only what the change can break

```bash
dev test                 # 1) the tests that failed last time; 2) if they pass, the tests affected by your changes
dev test plan            # what would run and why (changed file → tests), with the time estimate; runs nothing
dev test run --failed    # only the last failures
dev test run --all       # the full suite (once, before the push)
dev ci --run             # what CI runs, locally, with evidence for dev push
dev ci --run --resume    # after a fix: only the failed steps and what the fix affects
```

**Blast radius.** devkit maps the changed files to the tests that can see them: a changed test file selects itself; a
changed source file selects the tests that import it (directly or through other modules); docs select nothing; and a
change to build files, lockfiles, test configuration or shared fixtures (`conftest.py`, `jest.config.*`, `pom.xml`,
`package.json`, `go.mod`, …) selects everything, because everything can break. When the radius is everything, devkit
says which file caused it and how long the full run takes, instead of starting hours of tests silently.

The loop:

1. Change code, run `dev test` (seconds to minutes).
2. A test fails: fix it, run `dev test` again. It starts with the failures, so you know at once whether the fix worked.
3. Done with the change: run the full suite **once** (`dev ci --run`).
4. A step fails there: fix it, `dev test run --failed`, then `dev ci --run --resume`. Never relaunch everything to check
   one fix.

The native equivalents, when you run a tool directly: `pytest --lf` / `--ff` / `-x`; `jest --onlyFailures`,
`--findRelatedTests <files>`, `--onlyChanged`; `vitest related <files>`; `mvn -Dtest=A,B
-Dsurefire.failIfNoSpecifiedTests=false`; `gradle test --tests A` (with the build cache on); `go test ./pkg/...` (the
test cache skips unchanged packages); `phpunit --filter`, `--order-by=defects --stop-on-defect`.

## 5. Flaky tests

A test that passes on retry is a bug in the test or in the code. Reproduce it (`pytest --count 50 -x` with
pytest-repeat, `go test -count=100 -run X`, a loop over `jest -t X`), find the cause (shared state, order, time, async
waits, ports, randomness), and fix it. While you fix it, quarantine it with a task (`dev task add … --type bug`) and a
skip that names the task. Never retry until green: `dev run rerun --flaky "<evidence>"` is only for a failure proven to
be flaky, and the flaky test goes in the handoff note.

## 6. Measure whether tests are useful

- **Diff coverage, not global coverage:** the lines you changed must be covered (`diff-cover` with the coverage report).
  A global percentage is a floor, never a goal.
- **Mutation testing on the changed module**, not the whole repository: mutmut or cosmic-ray (Python), Stryker
  (JavaScript, TypeScript, C#), PIT (Java), Infection (PHP), go-mutesting (Go). A surviving mutant is a behavior no test
  checks: add the assertion that kills it, or accept it with a reason.
- **History:** a test that never failed in a year and duplicates another one is a candidate for deletion.

## 7. Diagnose an existing suite (old or external projects)

1. `dev test plan --all` and the last CI runs (`dev ci status`): how many tests, how long, which are slowest.
2. `dev test audit` on the whole suite: tests that cannot fail, skips, sleeps, duplicates.
3. Flaky history: the CI reruns, the retries in the pipeline config, and the tests marked flaky or skipped.
4. A mutation-testing sample on the most important module.
5. Propose, in this order: delete tests that cannot fail; fix flaky ones; move slow ones to an integration job;
   enable caching and parallelism in CI (`cicd` skill, `dev ci audit`); then add the missing tests where mutants survive.

Record the findings as tasks (`dev task add`) and lessons when a mistake could repeat (`learning-loop`).

## Done when

- Every new test fails without your change (`dev test prove`) and passes with it.
- `dev test audit --changed` reports no high findings.
- The affected tests pass (`dev test`), and the full suite passed once before the push (`dev ci --run`).
