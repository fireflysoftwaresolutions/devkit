# The crew: devkit's specialist agents

devkit gives Claude Code and Codex a crew of 25 specialist agents, named after characters of The Matrix, plus
Morpheus, the role the main session takes when it leads them. Each member has one professional role, a short
personality, the skills it loads, the files it may change, and what it returns. One file, `config/crew.json`, defines
them for every surface, and devkit coordinates them: plan steps with dependencies, a RACI per kind of work, one
progress lane per member with its own time, file scopes the guard enforces, messages between members, and checks
every member's report must pass.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/diagrams/crew-coordination-dark.svg">
  <img src="images/diagrams/crew-coordination-light.svg" alt="How Morpheus leads the crew: the developer gives Morpheus, the main session, a goal; Morpheus plans the steps with their dependencies and briefs crew members, each in its own lane with its own files, up to the slot limit (four by default); members send questions and decisions back to Morpheus, who asks the developer the decisions that are theirs; at SubagentStop devkit checks each report before it reaches Morpheus: the member's status block, its recorded step, and the crew gates; after the push, CI watch follows the run and sends a failure's diagnosis to Morpheus, who hands the fix to Link." width="100%">
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

1. **Plan with dependencies,** and declare each step's kind of work in brackets:

   ```bash
   dev progress start "Invoice export" --plan "requirements=20m [requirements]; \
     design=30m after requirements [architecture]; api=1h after design [backend]; \
     ui=1h after design [frontend]; tests=30m after api, ui [testing]; review=20m after tests [review]"
   ```

2. **Assign the steps that go to a member,** with the files it owns:

   ```bash
   dev progress assign api --to Tank --scope "services/invoices/**,tests/invoices/**" --activity backend
   dev progress assign ui --to Trinity --scope "web/src/invoices/**" --activity frontend
   ```

3. **Brief and launch** them, all parallel members in one message:

   ```bash
   dev crew brief Tank "Add the CSV export endpoint" --scope "services/invoices/**" --step api --activity backend
   ```

   The brief has the member's identity, the task, the scope, the skills to load, the known pitfalls of its kind of
   work and files, what to return and how to report, and it prints the exact launch line for Claude Code and for
   Codex.

4. **Follow and answer.** `dev crew graph` draws the session as one graph: the steps with their state, owner, and
   kind of work, the lanes under them, decisions waiting for the developer, approvals owed, and open messages. Answer
   questions as they come (`dev crew answer M2 "…"`), and keep the developer informed with `dev progress step`, whose
   update shows every lane.

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

## Kinds of work and the RACI

Every piece of work has a kind: one of the 34 ids in `config/crew.json`, such as `backend`, `ux`, `cicd`, or `docs`.
For each kind, the crew's RACI names who does it (Responsible), who approves it (Accountable), who is consulted before
or during it, and who is informed after it.

```bash
dev crew raci              # the whole matrix
dev crew raci backend      # one kind of work
dev crew raci Tank         # what one member does, approves, and is consulted or informed on
```

devkit finds a step's kind of work from anchors that mean the same in any language, in this order:

1. **Declared:** `--activity <id>` on `dev progress assign` or `dev crew brief`, or `[id]` after a step in a plan
   (`api=1h after design [backend]`), on `dev progress start --plan` and `dev progress add`. An unknown id is refused.
2. **The member's role:** the Responsible kind of work of the member the step is assigned to.
3. **The files:** the files in the step's scope, such as `*.tsx` for frontend work, migrations for database work, and
   workflow files for CI/CD.

Only when none of these answers does devkit fall back to the English words of the step's name. That guess can still
refuse a step written in another language, and removing it is planned. Declare the kind of work, and the language of
the step's name does not matter.

The kind of work decides three things:

- **Who may take the step.** `dev progress assign` refuses a member the RACI does not make Responsible, unless
  `--why` gives the reason, which is recorded (`raci.override` in the audit trail).
- **Who approves it.** `dev progress done` refuses to end the run until each Accountable member has approved the work
  they own; `--why` ends it without that approval and records the reason.
- **What the brief carries.** The skills, knowledge, and known pitfalls of that kind of work.

**Approvers.** The developer decided that nobody approves their own kind of work. In the team's RACI, compliance is
approved by The Keymaker and incidents by Morpheus. devkit does not yet refuse a member who approves its own kind of
work: that check is planned.

## How devkit coordinates them

### Lanes and time

Every member gets its own progress lane: the SubagentStart hook opens it, and SubagentStop closes it. Its time is its
own active time, and a step assigned to it is done when it finishes. The plan's estimate becomes a critical path:
lanes run in parallel and dependencies wait, so the ETA counts the longest chain, not the sum (see
[Progress and communication](progress-and-communication.md#time-and-estimates)).

One launch makes one lane. Each launch is recorded under the ID of the tool call that made it, and its lane binds to
that launch alone, so two members launched together never swap lanes:

- **The step it names.** A bound lane takes the ready step its launch's description names, never just the member's
  first step.
- **A launch that failed** (an unknown agent type, an interrupt) starts no agent, so its record goes at once: it
  never labels another agent or holds a slot.
- **A resumed agent** keeps its own lane and takes nothing else.
- **A launch whose record was lost** gets a restricted lane: read-only, no step, no scope, no slot, recorded as
  `crew.unbound`, until Morpheus assigns it a step.

### Who is calling

Claude Code and Codex pass `agent_id` to every hook fired inside a subagent, and devkit uses it in two ways.

**A member's commands go to its lane.** devkit sends a member's `dev progress` and `dev crew` commands to that member's
lane, so a subagent can never advance the main plan by mistake. From a lane, `dev progress start`, `step`, `note`, and
`add` change the member's own lane. A member runs these commands on their own: inside a compound command (`&&`, a
pipe, `$(…)`, a redirect) the guard refuses them. Quoted text, such as a plan with `;` between its steps or a commit
message that mentions a command, is fine. Read-only crew commands run anywhere, alone or in a compound command:
`dev crew` with no subcommand, `graph`, `show`, `rules`, `history`, `next`, `raci` without `--set`, `sync --check`,
and any `--help`. Commands that change the team's crew or brief a member (`dev crew set`, `raci --set`, `brief`) are
Morpheus's, and `dev crew sync` is refused outside the member's step scope.

**Morpheus's commands must come from Morpheus.** The PreToolUse hook records every shell call with the agent that
made it, from the hook's own input (the call ledger, in devkit's state, closed when the call ends). A `dev` command
then looks for the open calls that hold its own arguments: it acts as Morpheus only when every such call is the main
session's, and as a member when any is a member's. No environment variable, parent process, or recent activity can
make a caller Morpheus. These commands need Morpheus:

- `dev progress start` and `start --replace` in the main session, and `assign`, `begin`, `reopen`, `skip`, and `done`;
- `dev crew route --plan --apply`, `decide`, and `mode`.

Type them yourself. Run through a script, `bash -c`, or another program, no open call holds them as typed, so devkit
refuses them and says how to run them (`caller.refused` in the audit trail). On Codex, devkit cannot verify the caller
yet: these calls are allowed and recorded as `caller.unverified`. Inside a subagent, the guard also refuses setting,
exporting, or unsetting a `DEVKIT_` variable, and `env -i` or `env -u`.

### Helpers

A member may launch a helper agent for a narrow lookup or a slice of its own files. devkit holds the helper to its
member's limits:

- the member's tool level and files, never more: a helper of a review-only member cannot edit, and a helper with a
  scope of its own stays inside both scopes;
- no plan step of its own;
- one crew slot, like any member;
- no launch deeper than `progress.max_depth` levels below Morpheus (2 by default, which allows Morpheus's members
  and then their helpers).

When a member needs another specialist, it asks Morpheus instead of launching one.

### Scratch folders

In the session's scratchpad, each member writes and deletes only in its own folder: `scratchpad/<member id>/`, or
`scratchpad/<member id>-<task>/` for one task (`scratchpad/tank-f3/`). The guard checks the file tools and what a
shell command writes or deletes (`rm`, `mv`, redirects, `find -delete`, nested shells). A helper uses its member's
folder. The shared top level and the other members' folders hold their work, so a cleanup by one member can never
delete another's files.

### Messages

Any member can ask anything, of Morpheus (the default) or of another member:

```bash
dev crew ask "Should the export include voided invoices?"           # to Morpheus
dev crew ask --to Seraph "Please look at the rounding in total()"   # to another member
dev crew answer M3 "No: only issued and paid ones"
dev crew inbox
```

A message reaches its recipient with the recipient's next action (the PostToolUse hook adds it to the context), or
with the main session's next prompt. A message to a member who has not started yet waits for them.

### Every report is checked

Every member ends its report with its own status block:

```markdown
**[Firefly> DevKit · Tank]** billing · `feat/invoice-export` · CSV export endpoint
- **Done:** added GET /invoices/export with streaming CSV and a contract test.
- **Check:** ✓ 18 tests pass, including 3 integration tests against Postgres
- **Next:** Trinity can call it; the endpoint is documented in the OpenAPI file.
```

When a member someone launched stops, the SubagentStop hook checks, in this order, and sends the member back once for
each check that fails, naming every missing item and the command that fixes it:

1. **The status block.** The report starts with the member's own block, the way the Stop hook checks the main
   session's.
2. **The step's record.** The member recorded its work with `dev progress step "…" --check "…"`.
3. **The crew gates,** read from the member's own transcript:

   | Gate | What the member owes |
   | --- | --- |
   | Proofs | Each proof command its procedure names ran as a shell command (text in quotes or in a heredoc does not count) |
   | Skills | The skills of each procedure phase it worked in, and the skills devkit's rules require for the files it changed |
   | Unfinished work | No stubs, placeholders, or untracked TODOs in the files it changed |
   | Tests | No test that cannot fail in the test files it changed |
   | Lessons | Two or more guard refusals answered with `dev lesson add` or `dev lesson recur` |

Members with read-only tools have no shell, so they skip the proofs and lessons gates. The unfinished-work and test
gates count only what the member wrote itself: another member's uncommitted lines in a shared file are not its
findings. Every gate finding goes to the audit trail as `crew.gate`. With `crew.gates.enforce` set to `false`, the
gates only record; with `canary.enforce` set to `false`, the first two checks do not send the member back. After the
one send-back, the member's step closes when it stops, even if findings remain; keeping it open is planned.

`dev canary` and `dev doctor` count the crew's reports apart from the main sessions' (`crew 12/13`), so you can always
see that the kit's rules still hold inside every agent. On Codex these checks are not verified yet (see
[Surfaces](surfaces.md#what-not-yet-means-for-codex)).

### CI failures go to Link

After Morpheus pushes or opens a pull request, `dev ci watch` follows the run in the background. A failure reaches
Morpheus as a crew message from CI watch, with the failed job, the first error, its class, and one next action. When
the fix is not trivial, Morpheus hands it to Link (`dev crew brief link "Fix the failed CI run: <job>, <class>"
--activity cicd`), at once in auto mode. Routing sends work on workflow files to Link too. See
[CI/CD pipelines › Watch every run](ci-cd.md#watch-every-run).

## Who does what: routing, delegation, and dispatch

Members can launch narrow helpers but not other crew members, so the crew coordinates through the run's plan, and
Morpheus is the one dispatcher.

```bash
dev crew route "add a nullable column for the export date"   # who fits best, with the reasons
dev crew route --plan --apply                                 # a member for every plan step nobody owns
dev crew delegate "add the export column" --to Dozer          # from anyone: a new step for another member
dev crew delegate "review the parser before merge" --to auto  # routed to the best fit
dev crew decline "add the export column" --why "needs a data migration plan first"
dev crew next                                                 # ready to launch, queued, waiting, busy
dev crew graph                                                # the whole session as one graph
```

- **Routing** suggests a member: `dev crew route` ranks the members by their keywords (`config/crew.json`), their
  role, and the files the task touches (`*.tsx` → Trinity, migrations → Dozer, workflow files → Link, Terraform →
  Niobe, tests → Mouse, docs → Ghost). With no clear match the work goes to Neo, the generalist. The keywords are
  English, so a route is a suggestion; what devkit enforces rests on the [kind of work](#kinds-of-work-and-the-raci).
- **Delegation** adds a step to the plan with who asked for it. If the member is already working, the step joins its
  lane and the member gets a message (it can decline with a reason, and the step goes back to Morpheus). Otherwise
  Morpheus is told to launch it.
- **Dispatch:** when a step's dependencies are done and its member is not working, Morpheus gets "ready to launch"
  with the exact `dev crew brief …` command, once, within the slot limit; in auto mode he launches all ready members
  at once. At any time, `dev crew next` shows this list with every waiting step and its reason, and the current slot
  limit.

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

Each guardrail cites the rule of `dev crew rules` that enforces it. A rule marked planned is not checked yet.

| Guardrail | What happens |
| --- | --- |
| Limited parallel members | a launch past `progress.max_parallel` (4 by default) is refused with who is working; resumed agents and members' helpers count, and the limit is 1 while the 5-hour usage is over `progress.quota_percent` (`E-parallel`, `E-resumed`) |
| Separate files for parallel members | `assign` refuses a scope that overlaps a working member's scope (`E-separate-files`) |
| A member stays in its scope | the guard refuses its edits outside the files it was assigned (`E-scope`); the same check for shell writes is planned (`E-shell-scope`) |
| The main session respects a member's files | the guard refuses the main session's edits inside a working member's scope; `DEVKIT_SCOPE_OVERRIDE=1` for an emergency (`E-main-scope`) |
| Read-only members never edit | Seraph, Agent Smith, The Merovingian, Persephone, The Oracle, Councillor Hamann, and Sparks have no edit tools, and their helpers have none either (`E-read-only`) |
| Helpers stay inside their member's limits | a helper gets its member's tools and files, takes a slot, and no launch goes deeper than `progress.max_depth` (`E-nested`) |
| Work goes to its Responsible member | `assign` refuses another member unless `--why` is recorded, judged on the declared kind of work, the member's role, or the files (`E-responsible`, `E-any-language`); with none of them, English words can still refuse a step, and removing that is planned (`E-words-never-refuse`) |
| Every kind of work has an approver | the run ends only after each Accountable member approved (`E-approvals`); an explicit verdict, and refusing self-approval, are planned (`E-verdict`, `E-no-self-approval`) |
| Steps change only with evidence | closing a step needs `--check`, and a step whose dependencies are open is refused (`E-deps`); a member's step still closes when the member stops, and requiring its evidence there is planned (`E-evidence`) |
| Mistakes are fixable and visible | `dev progress reopen` or `dev progress skip` with `--why`, and `dev progress add`; every change is in `dev progress history` (`E-fixable`) |
| A member that never reports back | after `progress.stale_agent_hours` (6) its lane is closed as lost and its step goes back to the main session (`E-lost-lane`) |
| Every report is checked | the status block, the step's record, and the crew gates at SubagentStop (`E-status-block`, `E-step-records`, `E-proofs`, `E-skills`, `E-stubs`, `E-tests-fail`, `E-lessons`) |
| Only Morpheus plans, assigns, and decides | from a lane, `assign`, `begin`, `reopen`, `skip`, `done`, `decide`, and `mode` are refused, and in the main session they must be typed as commands, not run by a script (`E-main-only`, `E-caller`) |
| Decisions that are yours | guided mode waits for you (`E-decisions`); the gate that holds irreversible actions for your word is planned (`E-irreversible`) |
| devkit's own state is protected | the guard refuses agents' writes to devkit's state and configuration, and a forged `HOME`, `DEVKIT_CONFIG`, or `dev` link (`E-protected`, `E-forged-env`) |

## Everything is recorded

Every coordination event lands in two places: the run's history (`dev progress history`), which the status, the
dashboard and the handoff read, and devkit's audit trail (`dev audit`), published daily to the handoff repository:

| Audit action | When |
| --- | --- |
| `plan.done`, `plan.running`, `plan.pending`, `plan.blocked`, `plan.skipped` | every step change, by Morpheus, a member, or devkit |
| `crew.start`, `crew.finish`, `crew.lost` | a member starts, finishes (with its active minutes), or stops reporting |
| `crew.bind`, `crew.resume`, `crew.launch-failed`, `crew.unbound` | a lane binds to its launch, a member resumes, a launch fails, or a lane runs restricted because its launch record was lost |
| `crew.message` | a question or answer between members |
| `crew.decision`, `crew.decided`, `crew.mode` | a decision point, its answer (and by whom), a change of mode |
| `crew.delegate`, `crew.decline` | work handed to another member, or handed back |
| `raci.override` | a step assigned to a member the RACI does not make Responsible, with the reason |
| `caller.refused`, `caller.unverified` | a Morpheus command refused because it did not come from Morpheus, or allowed on Codex without verification |
| `guard.deny` with `rule: crew-scope` | an edit outside a member's files |
| `canary.crew-miss`, `crew.unreported`, `crew.gate` | a member's report without its status block, a member that stopped without `dev progress step`, or a crew gate finding |

```bash
dev audit --action crew        # the crew's coordination
dev audit --action plan        # every step change
```

## Changing the crew

devkit ships the roster and the RACI in `config/crew.json`. A team changes them in the handoff repository
(`crew/crew.json`), with who and why, and every session uses those changes:

```bash
dev crew set Tank skills "backend; java-springboot" --why "the services are Spring"
dev crew raci compliance --set "R=persephone A=keymaker C=… I=…" --why "nobody approves their own work"
dev crew history                 # the team's changes, newest first
```

To change what devkit ships for everyone, edit `config/crew.json` in the devkit repository: a member's role, summary,
personality, `use_when`, skills, tools (`all`, `build`, `review`, `read`), or tier (`strong`, `standard`, `fast`).
Then:

```bash
dev crew sync                    # regenerate the Claude Code agents
dev install --only claude,codex  # reinstall the plugin and write the Codex roles
```

`dev crew sync --check` reports generated files that are out of date; `dev doctor` runs it too. Never edit
`plugin/agents/*.md` by hand: the next sync overwrites them.

## Keeping the rules true

`dev crew rules` lists every rule the crew is given, the devkit check that enforces it, and its state: enforced,
planned (with the plan step that adds it), or guidance. It exits with `1` when a rule's check is missing. The
consistency checks in devkit's test suite compare the rules, the commands, the hooks, the configuration keys, and
these pages with each other, so they cannot drift apart (see
[Developing devkit › Consistency checks](development.md#consistency-checks)).

See also [Progress and communication](progress-and-communication.md), [CI/CD pipelines](ci-cd.md),
[Guard rules](guard.md), and [Agents](agents.md).
