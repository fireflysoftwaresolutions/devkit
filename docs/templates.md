# Templates

devkit gives every record one shape, so anyone (a teammate, an agent, you next month) can read any record quickly.
The commands fill the templates for you; you replace the placeholders. This page lists every template, where it
lives, and which command uses it.

| Template | Files in the devkit repository | Used by |
| --- | --- | --- |
| Plans and specs | `templates/plans/{plan,spec,spike,bugfix,adr}.md` | `dev plan new --kind …` |
| Work items | `templates/tasks/{task,feature,bug,spike}.md` | `dev task add --type …` |
| Pull request description | `templates/github/pull_request.md` | Agents, through the `communicating` skill |
| Handoff repository README | `templates/handoff/README.md` | `dev install` when it creates a handoff repository |

Placeholders are in italics with underscores, `_like this_`. Fill every one, or delete the section when it does not
apply. A finished record never contains template text.

## Plans and specs

`dev plan new "<title>" --kind <kind>` writes `plans/<scope>/<date>-<slug>.md` in the handoff repository from one of
five templates, replacing `{{title}}` and `{{date}}`. The plan starts at version 1 as a draft; after you change it,
record why with `dev plan revise <file> --why "…"` (see [Teams and machines › Versioned
plans](teams.md#versioned-plans)).

| Kind | Use it for | Sections |
| --- | --- | --- |
| `plan` (default) | A change that spans many files or days | Summary · Context · Goals · Non-goals · Approach · Steps (table: step, owner, estimate, verified by) · Verification · Risks (table: likelihood, impact, mitigation) · Rollout and rollback · Open questions |
| `spec` | A feature, before anyone builds it | Summary · Problem · Users and scenarios · Requirements (a table of R1…, with must, should, or could) · Out of scope · User experience · API and data · Acceptance criteria (Given … when … then …) · Metrics · Risks and open questions |
| `spike` | A time-boxed question | Question · Timebox · Options to evaluate · Method · Findings · Recommendation (with confidence) |
| `bugfix` | A bug worth a written fix plan | Symptom · Reproduction · Expected vs. actual · Root cause (with `file:line`) · Fix · Verification · Prevention |
| `adr` | An architecture decision record | Status · Deciders · Date · Context · Decision · Options considered (table: pros, cons) · Consequences |

```bash
dev plan new "Invoice export" --kind spec
dev plan new "Use Kafka for refund events" --kind adr -i payments
```

A repository that keeps its own ADRs (for example in `docs/adr/`) keeps them there; the handoff copy is for work that
spans projects or has no home yet.

## Work items

`dev task add "<title>" --type <type>` gives the new item a description from the template of its type. `--why` fills
the first section and `--done` (repeat it for each criterion) fills the `Done when` checklist. `--body "…"` uses
your own text instead, and `--no-template` leaves the description empty.

| Type | Id prefix | Sections |
| --- | --- | --- |
| `task` (default) | `T-` | Context · Done when |
| `feature` | `F-` | Why · Scope · Out of scope · Done when |
| `bug` | `B-` | Symptom · Steps to reproduce · Expected vs. actual · Environment · Done when (a failing test reproduces it and now passes; the root cause is fixed and noted) |
| `spike` | `S-` | Question · Timebox · Done when (findings and a recommendation are written down) |

```bash
dev task add "Export invoices as CSV" --type feature --now \
  --why "Finance copies invoices into a spreadsheet by hand every month" \
  --done "GET /invoices/export streams CSV" --done "Finance tried it on October's data"
```

When placeholders are left, `dev task add` prints the command that replaces the description:
`dev task edit <id> --body-file <file> --why "…"`.

## Pull request description

devkit's pull request template comes in English, `templates/github/pull_request.md`, and in Spanish,
`templates/github/pull_request.es.md` (Qué y por qué, Cambios, Cómo verificarlo, Riesgo y vuelta atrás, Enlaces). Its
sections are the ones the `communicating` skill asks agents to fill:

| Section | What goes in it |
| --- | --- |
| What and why | Two or three plain sentences for a reviewer who has not seen the work |
| Changes | The changes a reviewer must know about, most important first, one line each |
| How to verify | The commands you ran and their results (`./mvnw verify` → 214 tests passed), then how a reviewer checks it |
| Risk and rollback | What could break, who would notice, how to undo it. "Low: docs only" is a fine answer |
| Links | Task, issue (`Closes #…`), plan, design document, related PRs |

### How `dev pr create` uses it

1. **It picks the template.** The repository's own template wins: `.github/pull_request_template.md`, or the other
   places GitHub reads (`.github/PULL_REQUEST_TEMPLATE.md`, the repository root, `docs/`). Otherwise it takes devkit's
   in your `language`, trying `pull_request.<language>.md`, then `pull_request.<base language>.md`, then
   `pull_request.md`; `--lang` overrides it for one PR.
2. **It fills what it knows.** Changes gets one line per commit (WIP commits left out). How to verify gets the local
   CI result, `Local CI: dev ci --run → passed (2 steps: Lint, Unit tests) @ d75a9f9`, or the note of
   `dev ci --mark-passed`. Links gets the task ids (`T-…`), ticket keys (`PROJ-142`), and `Closes #n` found in the
   branch name and the commits.
3. **It checks what only you can write.** "What and why" and "How to verify" are required: comments, empty bullets,
   and `<placeholders>` do not count as content. A PR with either section empty is refused, unless it is a `--draft`;
   `dev pr ready` checks again before a draft goes to review. Headings are recognized by meaning, in English and
   Spanish ("Summary," "Testing," and "Resumen" count too).
4. **It removes AI attribution** from the body.

`dev pr create --dry-run` prints the pre-filled body and which required sections are still empty; fill them and pass
the file with `--body-file`, or use `--edit` to fill them in your editor. When neither the repository nor devkit has a
template, a built-in copy of the English one is used.

The PR title is a Conventional Commit (`feat(invoices): export invoices as CSV`, at most 72 characters), or a plain
imperative sentence in a repository whose recent PR titles are sentences, and the
squash-merge message repeats it with the "What and why" in one or two sentences and no attribution lines.

## Handoff repository README

`templates/handoff/README.md` is the README of every new handoff repository. It explains, for anyone who opens the
repository on GitHub, what each file and folder is, the rules for a team of developers and machines, the anatomy of a
note, and the everyday commands. `dev install` copies it when it creates the repository, and adds it to an existing
one that has no README; it never overwrites one.

## Formats that have no file

| Record | Shape | Where it is defined |
| --- | --- | --- |
| Status block | A bold `[Firefly> DevKit]` header with project, branch, and task, then Done, Check, Progress, and Next | [Concepts › The status block](concepts.md#the-status-block) |
| Run steps | `dev progress start --plan "a; b; c"`: a checklist with ✓ done, ▸ current and ○ waiting | [Progress and communication](progress-and-communication.md) |
| Progress update | `dev progress step --check "…" --next "…"` prints the status block with the Progress bullet, then the checklist | [Progress and communication](progress-and-communication.md#live-progress-with-dev-progress) |
| Session line | `YYYY-MM-DD HH:MM · user@host · agent · …` | [Teams and machines › Session logs](teams.md#session-logs) |
| Delegation message | The ask, why it matters, what done means, the deadline, where the task is, how to pick it up | [Teams and machines › Hand a task to a teammate](teams.md#hand-a-task-to-a-teammate) |
| Status report | `dev report`: a summary table, then per initiative health, progress, done, in progress, next, risks, and team | [Progress and communication › Status reports](progress-and-communication.md#status-reports) |
| Initiative note | `dev initiative new` writes Goal, Scope, Current state, Workstreams, Tasks, Decisions and gotchas, Risks and open questions, Links | [Initiatives](initiatives.md) |
| Lesson | `dev lesson add` writes a front matter block, then What happened, Root cause, Fix, Prevention, Detection, Evidence, and a changelog | [Lessons](lessons.md#write-it) |
| Commit message | `type(scope): summary` in the imperative, at most 72 characters, then the reason in the body | [Git and GitHub › Commits](git-and-github.md#commits) |
| Audit record | One JSON line: time, user, machine, agent, session, action, target, detail, outcome | [Scheduled jobs and the audit trail](jobs-and-audit.md#the-audit-trail) |

## Change a template

The templates are plain Markdown in the devkit repository. Edit them in a branch and follow
[Developing devkit](development.md): `dev plan new` and `dev task add` read them on every run, so the change applies as
soon as the devkit checkout has it. Keep `{{title}}` and `{{date}}` in plan templates and the `## Done when` heading in
task templates; `--why` and `--done` look for them.

See also [Teams and machines](teams.md), [Progress and communication](progress-and-communication.md), and
[Initiatives](initiatives.md).
