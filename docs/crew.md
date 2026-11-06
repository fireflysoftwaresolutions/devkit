# The crew: devkit's specialist agents

devkit gives Claude Code and Codex a crew of 25 specialist agents, named after characters of The Matrix, plus
Morpheus, the role the main session takes when it leads them. Each member has one professional role, a short
personality, the skills it loads, the files it may change, and what it returns. One file, `config/crew.json`, defines
them for every surface, and devkit coordinates them: plan steps with dependencies, one progress lane per member with
its own time, file scopes the guard enforces, messages between members, and a status block every member must end with.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/diagrams/crew-coordination-dark.svg">
  <img src="images/diagrams/crew-coordination-light.svg" alt="How Morpheus leads the crew: the developer gives Morpheus, the main session, a goal; Morpheus plans the steps with their dependencies and briefs crew members, each in its own lane with its own files, up to four at once; members send questions and decisions back to Morpheus, who asks the developer the decisions that are theirs; at SubagentStop the crew canary checks each report for the member's status block before it reaches Morpheus; after the push, CI watch follows the run and sends a failure's diagnosis to Morpheus, who hands the fix to Link." width="100%">
</picture>

## Who is on the crew

| Member | Role | Tools | Tier |
| --- | --- | --- | --- |
| Morpheus | Tech Lead and delivery manager (the main session) | all | the session's |
| Councillor Hamann | Product owner | review | strong |
| The Oracle | Business and requirements analyst | review | strong |
| The Architect | Software architect | build | strong |
| Roland | Technical researcher | all | strong |
| Neo | Senior software engineer | all | standard |
| Trinity | Frontend engineer | all | standard |
| Tank | Backend and API engineer | all | standard |
| Dozer | Data and database engineer | all | standard |
| Switch | Mobile engineer | all | standard |
| Rama-Kandra | AI and ML engineer | all | strong |
| Kid | Developer experience and tooling engineer | all | standard |
| Sati | UX and visual designer | build | strong |
| Mouse | Test and quality engineer | all | standard |
| Seraph | Independent code reviewer | review | strong |
| The Keymaker | Security engineer | build | strong |
| Agent Smith | Adversarial tester (red team) | review | strong |
| Persephone | Compliance, privacy, and licensing reviewer | review | strong |
| Link | CI/CD and release engineer | all | standard |
| Niobe | Infrastructure and SRE | all | standard |
| Commander Lock | Incident commander | all | strong |
| The Merovingian | Performance and observability analyst | review | strong |
| The Trainman | Migration and modernization engineer | all | strong |
| Apoc | Data and analytics analyst | build | standard |
| Ghost | Technical writer | build | standard |
| Sparks | Fast read-only lookup scout | read | fast |

**Tools:** `all` reads, writes, and runs commands; `build` is the same without web access; `review` reads and runs
commands but never edits; `read` only reads and searches.

**Tier:** the model and effort a member runs with.

| Tier | Claude Code | Codex |
| --- | --- | --- |
| `strong` | the session's model at high effort | the `deep` profile's model at high effort |
| `standard` | the session's model and effort | the session's model and effort |
| `fast` | Haiku at low effort | the `fast` profile's model and effort |

Sparks, the only `fast` member, does mechanical lookups; `scout` is an alias for Sparks. The names are identities, not
role-play: every member writes plainly and professionally, in the developer's language (devkit config `language`,
American English by default).

`dev crew` prints the roster and who is working now; `dev crew show <name>` shows one member in full.

## How the crew reaches each surface

| Surface | How members are launched | What devkit installs |
| --- | --- | --- |
| Claude Code (terminal) and Claude Desktop's Code tab | Agent tool, `subagent_type: devkit:<id>` | `plugin/agents/<id>.md` in the devkit plugin |
| Codex CLI and the ChatGPT app | `spawn_agent` with agent type `<id>` | `~/.codex/agents/<id>.toml` |
| Claude Desktop chat and Cowork | not launched there | the `devkit_crew` tool of the devkit extension shows the crew, the live lanes, and open questions |

`dev crew sync` regenerates the Claude agents from `config/crew.json`, and `dev install --only codex` writes the Codex
roles. Edit a member there, never in the generated files.

## Leading the crew

The main session leads when work needs more than one kind of expertise. The `crew` skill is the full manual; the
short version:

1. **Plan with dependencies.**

   ```bash
   dev progress start "Invoice export" --plan "requirements=20m; design=30m after requirements; \
     api=1h after design; ui=1h after design; tests=30m after api, ui; review=20m after tests"
   ```

2. **Assign the steps that go to a member,** with the files it owns:

   ```bash
   dev progress assign api --to Tank --scope "services/invoices/**,tests/invoices/**"
   dev progress assign ui --to Trinity --scope "web/src/invoices/**"
   ```

3. **Brief and launch** them, all parallel members in one message:

   ```bash
   dev crew brief Tank "Add the CSV export endpoint" --scope "services/invoices/**" --step api
   ```

   The brief has the member's identity, the task, the scope, the skills to load, what to return and how to report,
   and it prints the exact launch line for Claude Code and for Codex.

4. **Answer questions** as they come (`dev crew answer M2 "…"`), and keep the developer informed with
   `dev progress step`, whose update shows every lane.

5. **Check and integrate** each member's work, then run the full suite once (`dev ci --run`).

### Parallel or in sequence

| Situation | Decision |
| --- | --- |
| Under about ten minutes of work, or you already hold the context | do it yourself |
| The next step needs this step's output | in sequence |
| Two parts change the same files or shared state | in sequence, or one member does both |
| Independent parts with fixed contracts and separate files | in parallel, one member per part |
| Reviews of the same change (Seraph, The Keymaker, Agent Smith) | in parallel |
| The interfaces between the parts are not settled | The Architect first, then in parallel |
| Hours of separate work | parallel sessions in worktrees (`dev wt new`) |

A member starts cold and uses a full context of its own, so parallel work costs more tokens: it pays when it saves
real time on independent work, and it is waste on small or coupled tasks. Follow-ups go to the same member (Claude
`SendMessage`, Codex `send_input`), which keeps its context.

## How devkit coordinates them

**Lanes and time.** Every member gets its own progress lane: the SubagentStart hook opens it, and SubagentStop closes
it. Its time is its own active time, and a step assigned to it is done when it finishes. The plan's estimate becomes
a critical path: lanes run in parallel and dependencies wait, so the ETA counts the longest chain, not the sum (see
[Progress and communication](progress-and-communication.md#time-and-estimates)).

**Who is calling.** Claude Code and Codex pass `agent_id` to every hook fired inside a subagent. devkit uses it to
send a member's `dev progress` and `dev crew` commands to that member's lane, so a subagent can never advance the
main plan by mistake. From a lane, `dev progress start`, `step`, and `add` change the member's own lane; `assign`,
`begin`, `reopen`, and `skip` change the main plan, so they are Morpheus's and refused there. A member runs these
commands on their own: inside a compound command (`&&`, a pipe, `$(…)`, a redirect) devkit cannot tell whose they
are, and the guard refuses them. Quoted text, such as a plan with `;` between its steps, is fine.

**Messages.** Any member can ask anything, of Morpheus (the default) or of another member:

```bash
dev crew ask "Should the export include voided invoices?"           # to Morpheus
dev crew ask --to Seraph "Please look at the rounding in total()"   # to another member
dev crew answer M3 "No: only issued and paid ones"
dev crew inbox
```

A message reaches its recipient with the recipient's next action (the PostToolUse hook adds it to the context), or
with the main session's next prompt. A message to a member who has not started yet waits for them.

**Reports and the canary.** Every member ends its report with its own status block:

```markdown
**[Firefly> DevKit · Tank]** billing · `feat/invoice-export` · CSV export endpoint
- **Done:** added GET /invoices/export with streaming CSV and a contract test.
- **Check:** ✓ 18 tests pass, including 3 integration tests against Postgres
- **Next:** Trinity can call it; the endpoint is documented in the OpenAPI file.
```

The SubagentStop hook checks it the way the Stop hook checks the main session's, and sends a member back once
without it. `dev canary` and `dev doctor` count the crew's reports apart from the main sessions' (`crew 12/13`), so
you can always see that the kit's rules still hold inside every agent.

**CI failures go to Link.** After Morpheus pushes or opens a pull request, `dev ci watch` follows the run in the
background. A failure reaches Morpheus as a crew message from CI watch, with the failed job, the first error, its
class, and one next action. When the fix is not trivial, Morpheus hands it to Link
(`dev crew brief link "Fix the failed CI run: <job>, <class>"`), at once in auto mode. Routing sends work on workflow
files to Link too. See [CI/CD pipelines › Watch every run](ci-cd.md#watch-every-run).

## Who does what: routing, delegation, and dispatch

Subagents cannot launch other subagents in Claude Code (and Codex limits how deep they go), so the crew coordinates
through the run's plan, and Morpheus is the one dispatcher.

```bash
dev crew route "add a nullable column for the export date"   # who fits best, with the reasons
dev crew route --plan --apply                                 # a member for every plan step nobody owns
dev crew delegate "add the export column" --to Dozer          # from anyone: a new step for another member
dev crew delegate "review the parser before merge" --to auto  # routed to the best fit
dev crew decline "add the export column" --why "needs a data migration plan first"
dev crew next                                                 # ready to launch, queued, waiting, busy
```

- **Routing** is deterministic: each member's keywords (`config/crew.json`), its role and the kind of work it is for,
  and the files the task touches (`*.tsx` → Trinity, migrations → Dozer, workflow files → Link, Terraform → Niobe,
  tests → Mouse, docs → Ghost). With no clear match the work goes to Neo, the generalist.
- **Delegation** adds a step to the plan with who asked for it. If the member is already working, the step joins its
  lane and the member gets a message (it can decline with a reason, and the step goes back to Morpheus). Otherwise
  Morpheus is told to launch it.
- **Dispatch:** when a step's dependencies are done and its member is not working, Morpheus gets "ready to launch"
  with the exact `dev crew brief …` command, once, within the four-member limit; in auto mode he launches all ready
  members at once. At any time, `dev crew next` shows this list with every waiting step and its reason.

## Guided or automatic

Each run has a mode. **Guided** (the default, or devkit config `work.mode`) means the decisions that are yours wait
for you. **Auto** means the crew goes on with the recommended option and records it for you to review. Irreversible
actions (push, PR, merge, release, deploy, deleting data, a production migration, spending, publishing, messages to
other people) wait for you in both modes, unless you pre-approved them.

```bash
dev progress start "Invoice export" --plan "…" --mode auto
dev crew mode auto --approve "push, pr"     # switch, and pre-approve what may happen without you
dev crew mode guided                        # back to asking first
dev crew decisions                          # every decision: pending, decided automatically, decided by you
dev crew decide D3 "B: reject the file"     # your answer (or an override of an automatic one)
```

How a decision travels:

1. A member reaches a decision point and runs `dev crew ask --decision "<question>" --options "A | B" --recommend A`.
2. **Auto:** the answer comes back at once, is recorded as decision D3, and the member goes on.
3. **Guided:** the member's step is blocked, the member stops and reports that it waits for D3, and no tokens are
   spent while it waits. Morpheus sees the decision with his next action and asks you with the question dialog of
   Claude Code or Codex. Your answer is recorded with `dev crew decide`, and Morpheus resumes the member with it; the
   member keeps everything it knew.
4. The run cannot be closed while a decision is pending, and the final report lists the automatic decisions.

## Guardrails

| Guardrail | What happens |
| --- | --- |
| At most four members at once | a fifth launch is refused with who is working (`progress.max_parallel`) |
| Separate files for parallel members | `assign` refuses a scope that overlaps a working member's scope |
| A member stays in its scope | the guard refuses its edits outside the files it was assigned |
| The main session respects a member's files | the guard refuses the main session's edits inside a working member's scope; `DEVKIT_SCOPE_OVERRIDE=1` for an emergency |
| Read-only members never edit | Seraph, Agent Smith, The Merovingian, Persephone, The Oracle, Councillor Hamann, and Sparks have no edit tools |
| No nested launches | members ask Morpheus for another specialist instead of launching one |
| Steps change only with evidence | closing a step needs `--check`; a description that does not match the step, a step a member still owns, or a step whose dependencies are open is refused |
| Mistakes are fixable and visible | `dev progress reopen` or `dev progress skip` with `--why`, and `dev progress add`; every change is in `dev progress history` |
| A member that never reports back | after six hours its lane is closed as lost and its step goes back to the main session |
| Every report is checked | the crew canary at SubagentStop |
| Decisions that are yours | guided mode waits for you; irreversible actions wait in both modes unless pre-approved |
| Only Morpheus records your answers | `dev crew decide` is refused from inside a member |

## Everything is recorded

Every coordination event lands in two places: the run's history (`dev progress history`), which the status, the
dashboard and the handoff read, and devkit's audit trail (`dev audit`), published daily to the handoff repository:

| Audit action | When |
| --- | --- |
| `plan.done`, `plan.running`, `plan.pending`, `plan.blocked`, `plan.skipped` | every step change, by Morpheus, a member, or devkit |
| `crew.start`, `crew.finish`, `crew.lost` | a member starts, finishes (with its active minutes), or stops reporting |
| `crew.message` | a question or answer between members |
| `crew.decision`, `crew.decided`, `crew.mode` | a decision point, its answer (and by whom), a change of mode |
| `crew.delegate`, `crew.decline` | work handed to another member, or handed back |
| `guard.deny` with `rule: crew-scope` | an edit outside a member's files |
| `canary.crew-miss` | a member's report without its status block |

```bash
dev audit --action crew        # the crew's coordination
dev audit --action plan        # every step change
```

## Changing the crew

Edit `config/crew.json`: a member's role, summary, personality, `use_when`, skills, tools (`all`, `build`, `review`,
`read`), or tier (`strong`, `standard`, `fast`). Then:

```bash
dev crew sync                    # regenerate the Claude Code agents
dev install --only claude,codex  # reinstall the plugin and write the Codex roles
```

`dev crew sync --check` reports generated files that are out of date; `dev doctor` runs it too. Never edit
`plugin/agents/*.md` by hand: the next sync overwrites them.

See also [Progress and communication](progress-and-communication.md), [CI/CD pipelines](ci-cd.md),
[Guard rules](guard.md), and [Agents](agents.md).
