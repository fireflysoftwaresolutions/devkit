# Tutorial: a request from start to release

In this tutorial you take one request from the moment you type it to a published release, and you see the way
devkit means work to go: the tech lead plans it by kinds of work, routes each step to the crew member the RACI makes
Responsible, gives each member its own files, members work and report, reviewers approve, and the change ships with
one push and one CI run. It takes about an hour, most of it while the crew works.

The tutorial uses Claude Code in Terminal. Claude Desktop's Code tab works the same way; on Codex, the steps are the
same and the differences are in [The crew › How the crew reaches each surface](../crew.md#how-the-crew-reaches-each-surface).

## What you need

- devkit installed, with Claude Code chosen in the setup wizard and every line of `dev doctor` showing `✓`.
- GitHub signed in: `gh auth status` shows your login. The tutorial creates a private repository under your account.
- The first tutorial, [your first task with Claude Code](claude-code.md), done or read: this one assumes you know the
  status block.

## Step 1: create a project that can ship

A release needs a repository on GitHub with a `main` branch. Create one and start Claude Code in it:

```bash
dev new unit-convert --github
dcd unit-convert
claude
```

`dev new --github` creates the private repository `<your-login>/unit-convert` with a README on `main` and clones it
to `~/Development/<your-login>/unit-convert`, where devkit expects every GitHub repository.

## Step 2: make the request

Type this and press Return:

```text
Build a small Python package with a command-line tool: `convert 10 km mi` prints 6.21. Support kilometers, miles,
meters, and feet. Add tests, a GitHub Actions workflow that runs them, and a README section on how to install and
use it. Use the crew, and ship it as the project's first release.
```

The request holds five kinds of work: the package itself, its tests, a CI workflow, the docs, and a release.
"Use the crew" asks the tech lead to hand them to specialists; without it, he does work under ten minutes himself.

## Step 3: read the plan

The reply starts with the status block, then the plan. For example:

```markdown
**[Firefly> DevKit]** unit-convert · `main` · Unit converter
- **Done:** planned the work by kinds of work; similar work here took ~1h20m
- **Check:** – nothing to verify yet
- **Progress:** 0 of 7 steps · now 1. package · 0m active of ~1h30m · ETA ~1h00m–1h45m
- **Next:** the Backend engineer (Tank) builds the package

**Plan · Unit converter** — ~1h30m (7 steps)
1. package [backend] Backend engineer (Tank) 2. tests [testing] QA engineer (Mouse), after 1
3. ci [cicd] Release engineer (Link), after 2 4. docs [docs] Technical writer (Ghost), after 1
5. review [review] Code reviewer (Seraph), after 2 and 4 6. ci-review [security] Security engineer (The Keymaker), after 3
7. release [release] Release engineer (Link), after 5 and 6
```

Your plan may use other names and estimates. Here is how to read it:

- **One step per kind of work.** The id in brackets (`[backend]`, `[testing]`) is the step's kind of work, from the
  catalog `dev crew raci` lists. The tech lead declares it; devkit never guesses it from the words of your request,
  so the plan works the same in any language.
- **The RACI picks who does it and who approves it.** For each kind of work, one member is Responsible and one is
  Accountable. devkit refuses to assign a step to a member who is not Responsible for its kind of work, unless the
  tech lead records why.
- **`after` sets the order.** Tests wait for the package, the CI workflow for the tests. The docs wait only for the
  package, so they run in parallel with the tests. The ETA follows the longest chain, not the sum of the steps.
- **Every approver gets a review step.** The Code reviewer approves back-end and test work, and the Security engineer
  approves CI/CD work, so each has a step. The run cannot end until each has approved.

Check it yourself. Open a second Terminal window (Command-N) and ask devkit who does and who approves back-end work:

```bash
dev crew raci backend
```

```text
  ID       KIND OF WORK                R · DOES IT              A · APPROVES            C · CONSULTED                             I · INFORMED
  ───────  ──────────────────────────  ───────────────────────  ──────────────────────  ────────────────────────────────────────  ──────────────────────────────
  backend  Back-end services and APIs  Backend engineer (Tank)  Code reviewer (Seraph)  Software architect (The Architect), Sec…  Frontend engineer (Trinity), …
```

In the conversation you see the commands the tech lead runs to record the plan. They look like this:

```bash
dev progress start "Unit converter" --estimate 1h30m --plan "package=30m [backend]; tests=20m after package [testing]; \
  ci=15m after tests [cicd]; docs=15m after package [docs]; review=20m after tests, docs [review]; \
  ci-review=10m after ci [security]; release=15m after review, ci-review [release]"
dev progress assign package --to Tank --scope "src/**,pyproject.toml" --activity backend
dev crew brief Tank "Build the unit-convert package and its convert command" --scope "src/**,pyproject.toml" \
  --step package --activity backend
```

`assign` gives the step to its member with the files it owns, its **scope**. `dev crew brief` writes the member's
brief: the task, the scope, the skills to load, the known pitfalls, and the report format, with the launch line that
names the step.

## Step 4: watch the crew work

The tech lead launches the members whose steps are ready. Claude Code shows each one while it runs, with its
description, for example `Backend engineer (Tank): package`. While the crew works:

- **Each member works in its own lane.** A lane has its own step, its own active time, and its own scope: the guard
  refuses an edit outside it, so two members never write the same files. Each member also gets its own scratch folder
  for temporary files.
- **Each member records its step.** When it finishes, a member runs `dev progress step "…" --step package --check
  "<evidence>"`, for example the tests it ran. The step closes on that evidence, and the steps after it become ready.
  On a long step, it posts `dev progress note` with partial evidence.
- **Members ask, the tech lead answers.** A member with a question runs `dev crew ask`, and the tech lead answers
  with `dev crew answer`. A choice that is yours, such as whether `convert` rounds to two decimals, comes to you in
  Claude Code's question dialog: the options, the recommended one first. Click one; the member goes on with your answer.
- **Every report is checked.** A member's report must start with its own status block and pass the crew gates: it
  ran the proofs its procedure names, loaded its skills, left no unfinished work, and wrote tests that can fail. A
  report that misses one goes back to the member once.
- **The tech lead waits without polling.** Before he ends a turn while members work, he arms one wait,
  `dev watch --wait`, which wakes him for their questions, finished steps, and CI results.

You get a progress update after each finished step. To see the whole run at once, run this in the second window:

```bash
dev crew graph --latest
```

```text
  Session graph · <your-login>/unit-convert ─────────────────────────────────────
  ✓ done  ▸ running  ◇ ready  ○ waiting  ✗ blocked  – skipped  ! recheck · [kind of work] · after: the steps it waits for
  ● Tech lead (Morpheus) · main session · claude 3f2a9c1e
  ├─ Run · Unit converter
  │  running · mode guided · 1 of 7 steps · now 2. tests, 4. docs · 34m active of ~1h30m · ETA ~40m–1h05m · 2 agents working
  │  ├─ ✓ 1. package · done · Backend engineer (Tank) · [backend]
  │  ├─ ▸ 2. tests · running · QA engineer (Mouse) · [testing] · after 1
  │  │  └─ ● QA engineer (Mouse) · tests · working · 9m
  │  ├─ ○ 3. ci · waiting for 2 · Release engineer (Link) · [cicd] · after 2
  │  ├─ ▸ 4. docs · running · Technical writer (Ghost) · [docs] · after 1
  │  │  └─ ● Technical writer (Ghost) · docs · working · 6m
  │  ├─ ○ 5. review · waiting for 2, 4 · Code reviewer (Seraph) · [review] · after 2, 4
  │  ├─ ○ 6. ci-review · waiting for 3 · Security engineer (The Keymaker) · [security] · after 3
  │  └─ ○ 7. release · waiting for 5, 6 · Release engineer (Link) · [release] · after 5, 6
```

Two members work in parallel here because their steps do not depend on each other and their scopes do not overlap.

## Step 5: review and approval

When the tests and the docs are done, the Code reviewer reads the change. A review step starts even when the work it
follows ended unverified, and its Check `✓` approves the steps the reviewer is the approver for. A finding goes back
to the member who owns the files: the tech lead reopens that step, the member fixes it, and the review runs again.

The Security engineer reviews the CI workflow the same way: pinned actions and the least permissions the workflow
needs. If you ask the tech lead to finish while an approval is still owed, `dev progress done` refuses and names the
approver who is missing.

## Step 6: ship

The Release engineer takes the release step. devkit ships with one push and one CI run:

1. **Local CI first.** `dev ci --run` runs on your Mac the same commands the new workflow runs on GitHub, and records
   the result as evidence.
2. **One push, one pull request.** The work is on a branch such as `feat/unit-converter`. `dev push` pushes it once,
   only with that evidence, and `dev pr create` opens the pull request from devkit's template, filled with the
   commits and the local CI result. The workflow runs once on GitHub.
3. **The release policy.** Before the first release, the Release engineer records the project's delivery policy: how
   versions are numbered, what the changelog holds, and what a release publishes. `dev delivery` proposes one and
   `dev delivery apply` records it in the handoff repository. Without a versioning scheme of its own, the project uses
   CalVer, for example `26.11.0`.
4. **Merge and release.** `dev pr merge` merges the pull request and deletes its branch, and `dev release` tags the
   version and publishes the GitHub release with its changelog section.

Each action that leaves your Mac or cannot be undone, the push, the pull request, the merge, and the release, waits
for you. The tech lead asks in the question dialog, for example `D2 · Release v26.11.0 now?`, with the option that
runs it labeled `A: yes (release v26.11.0)`. Click that option to allow that one action on that target.

Follow CI from your second window while it runs:

```bash
dev ci status
```

## Step 7: finish

The final report starts with the status block, then says what changed, the evidence, and what is not done. When the
run ends, macOS shows a notification, and devkit records how long the work really took, so the next estimate for
similar work is closer.

devkit also updates the project's handoff note and session log in your private handoff repository. Check what the
next session starts from:

```bash
dev status unit-convert
```

It shows the project's latest status block, its tasks, and what needs you. A teammate or your next session, on this
Mac or another, starts from there.

## What you learned

| Stage | Who | What devkit checks |
| --- | --- | --- |
| Plan | Tech lead (Morpheus) | Each step declares its kind of work; `assign` refuses a member who is not Responsible |
| Build | The Responsible member, in its lane | The guard keeps its edits inside its scope; its step closes on evidence |
| Report | Each member | The crew gates: status block, recorded step, proofs, skills, no unfinished work, tests that can fail |
| Review | Each Accountable member | The run cannot end until each has approved |
| Ship | Release engineer (Link), with your approval | One push with local CI evidence, one CI run, `dev pr merge` and `dev release` |

See also [Concepts › How devkit fits together](../concepts.md#how-devkit-fits-together), [The crew](../crew.md), and
[Delivery](../delivery.md).
