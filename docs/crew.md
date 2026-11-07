# The crew: devkit's specialist agents

devkit gives Claude Code and Codex a crew of 25 specialist agents, each with a professional role and a name from The
Matrix, plus the tech lead (Morpheus), the role the main session takes when it leads them. Each member has one role,
a short personality, the skills it loads, the files it may change, and what it returns. One file,
`config/crew.json`, defines them for every surface, and devkit coordinates them: plan steps with dependencies, a
RACI per kind of work, the crew's slots, one progress lane per member with its own time, file scopes the guard
enforces, messages between members, and checks every member's report must pass.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/diagrams/crew-coordination-dark.svg">
  <img src="images/diagrams/crew-coordination-light.svg" alt="How the tech lead (Morpheus) leads the crew: the developer gives the tech lead, the main session, a goal; the tech lead plans the steps with their dependencies and briefs crew members, each in its own lane with its own files, up to the slot limit (four by default); members send questions and decisions back to the tech lead, who asks the developer the decisions that are theirs; at SubagentStop devkit checks each report before it reaches the tech lead: the member's status block, its recorded step, and the crew gates; after the push, CI watch follows the run and sends a failure's diagnosis to the tech lead, who hands the fix to the Release engineer (Link)." width="100%">
</picture>

## Who is on the crew

| Role (name) | What they do | Tools | Tier |
| --- | --- | --- | --- |
| Tech lead (Morpheus) | Tech lead and delivery manager (the main session) | all | the session's |
| Product owner (Councillor Hamann) | Priority, scope, and milestones | review | strong |
| Business analyst (The Oracle) | Business and requirements analysis | review | strong |
| Software architect (The Architect) | Design, contracts, and technology choices | build | strong |
| Technical researcher (Roland) | Spikes and evaluations | all | strong |
| Senior engineer (Neo) | Features and fixes across layers | all | standard |
| Frontend engineer (Trinity) | UI, accessibility, and front-end performance | all | standard |
| Backend engineer (Tank) | Services, APIs, and integrations | all | standard |
| Database engineer (Dozer) | Schemas, migrations, and queries | all | standard |
| Mobile engineer (Switch) | iOS, Android, and cross-platform apps | all | standard |
| AI engineer (Rama-Kandra) | LLM features, retrieval, and evaluations | all | strong |
| DevEx engineer (Kid) | Developer experience and tooling | all | standard |
| UX designer (Sati) | UX and visual design | build | strong |
| QA engineer (Mouse) | Tests and quality | all | standard |
| Code reviewer (Seraph) | Independent code review | review | strong |
| Security engineer (The Keymaker) | Threat models, auth, secrets, and supply chain | build | strong |
| Red team tester (Agent Smith) | Adversarial testing | review | strong |
| Compliance reviewer (Persephone) | Compliance, privacy, and licensing | review | strong |
| Release engineer (Link) | CI/CD and releases | all | standard |
| SRE (Niobe) | Infrastructure and site reliability | all | standard |
| Incident commander (Commander Lock) | Production incidents and postmortems | all | strong |
| Performance analyst (The Merovingian) | Performance and observability | review | strong |
| Migration engineer (The Trainman) | Migration and modernization | all | strong |
| Data analyst (Apoc) | Data and analytics | build | standard |
| Technical writer (Ghost) | Documentation, runbooks, and diagrams | build | standard |
| Lookup scout (Sparks) | Fast read-only lookups | read | fast |

**Tools:** `all` reads, writes, and runs commands; `build` is the same without web access; `review` reads and runs
commands but never edits; `read` only reads and searches.

**Tier:** the model and effort a member runs with.

| Tier | Claude Code | Codex |
| --- | --- | --- |
| `strong` | the session's model at high effort | the `deep` profile's model at high effort |
| `standard` | the session's model and effort | the session's model and effort |
| `fast` | Haiku at low effort | the `fast` profile's model and effort |

The Lookup scout (Sparks), the only `fast` member, does mechanical lookups; `scout` is its alias. The names are
identities, not role-play: every member writes plainly and professionally, in the developer's language (devkit
config `language`, American English by default).

**Roles first.** devkit writes a member as _Backend engineer (Tank)_ in everything it shows (the crew views, the
progress update, messages, decisions, refusals, briefs) and calls the main session the tech lead. Agents write them
the same way, or by the role alone, in any language: a final message or a member's report whose prose names a member
without the role is sent back once (`canary.roles`). Commands and code keep the ids (`--to tank`).

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
   Codex. The launch names the step, which is how the step binds to the member's lane.

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
| Reviews of the same change (the Code reviewer, the Security engineer, the Red team tester) | in parallel |
| The interfaces between the parts are not settled | the Software architect first, then in parallel |
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

Only when none of these answers does devkit fall back to the English words of the step's name, and words never refuse
a step: only a declared kind of work does. Declare the kind of work, and the language of the step's name does not
matter.

The kind of work decides three things:

- **Who may take the step.** `dev progress assign` refuses a member the RACI does not make Responsible, unless
  `--why` gives the reason, which is recorded (`raci.override` in the audit trail).
- **Who approves it.** `dev progress done` refuses to end the run until each Accountable member has approved the work
  they own; `--why` ends it without that approval and records the reason.
- **What the brief carries.** The skills, knowledge, and known pitfalls of that kind of work.

**Approvers.** An approval is an explicit verdict given after the work: the Check ✓ of a review step that follows it,
`dev progress step --approves "<steps>"`, or the developer's answer to a decision asked with `--approves`. Nobody
approves their own kind of work: when the Accountable member did the work, the Code reviewer (Seraph) approves code
and configuration, and the tech lead the rest. In the team's RACI, compliance is approved by the Security engineer and
incidents by the tech lead.

## How devkit coordinates them

### Lanes and time

Every member gets its own progress lane: the SubagentStart hook opens it, and SubagentStop closes it. Its time is its
own active time, and a step assigned to it is done when it finishes with evidence. The plan's estimate becomes a
critical path: lanes run in parallel and dependencies wait, so the ETA counts the longest chain, not the sum (see
[Progress and communication](progress-and-communication.md#time-and-estimates)).

One launch makes one lane. Each launch is recorded under the ID of the tool call that made it, and its lane binds to
that launch alone, so two members launched together never swap lanes:

- **The step it names.** A bound lane takes the ready step its launch's description names, never just the member's
  first step. `dev progress assign` binds a step to a lane that is already working only when that lane holds no other
  open step and its launch names the step; otherwise the step waits for a launch of its own, and the tech lead is
  told why.
- **The steps its evidence is about.** When a lane ends, it closes the step its launch names, a step its
  `dev progress step "…" --step "<name>"` names, and a step its report names. Any other step it held goes back to
  pending, still its member's, with the reason in the step's history. A step delegated into a working lane closes only
  on that lane's own evidence.
- **A launch that failed** (an unknown agent type, an interrupt) starts no agent, so its record goes at once: it
  never labels another agent or holds a slot.
- **A resumed agent** keeps its own lane and takes nothing else.
- **A launch whose record was lost** gets a restricted lane: read-only, no step, no scope, no slot, recorded as
  `crew.unbound`, until the tech lead assigns it a step.

### Slots

The crew shares a number of slots, and a launch that does not fit is refused with who is working:

- **The limit.** `progress.max_parallel` (4 by default), which a team's `policy/progress.json` can cap. Your
  recorded usage lowers it: `progress.quota_steps` keeps a share of the slots from 50, 70, and 85 % usage, the 5-hour
  and the weekly windows both count and the lower limit wins, and from `progress.quota_percent` (90 %) one member
  works at a time until the window resets.
- **The cost of a launch.** A builder costs one slot, a review-only or read-only member half a slot, and the
  Lookup scout (Sparks) nothing (`progress.slot_weights`). Resumed agents and members' helpers count.
- **The reserve.** `progress.reserve` (1) keeps a slot for what you ask while the crew works: plan steps queued in an
  earlier turn do not take it, and a launch for your request in the current turn does (`crew.reserve`).
- **The order.** `dev crew next` lists the ready steps in the order to launch them: the longest critical path first,
  then your latest request, then the oldest. It records that order, and a launch out of it needs
  `dev crew next --first "<step>" --why "…"`.

`dev crew`, `dev crew next`, and the status block's Crew line show the slots in use and why the limit is lower
("2 of 3 slots in use (lowered from 6: weekly usage at 89 %)"), and each change of the limit is audited
(`crew.limit`). A member stopped with TaskStop frees its slot at once, and its step waits unverified. You change
these keys in a terminal with `dev config set`; agents are refused. The keys are in
[Configuration](configuration.md#work-progress-and-display).

### Who is calling

Claude Code and Codex pass `agent_id` to every hook fired inside a subagent, and devkit uses it in two ways.

**A member's commands go to its lane.** devkit sends a member's `dev progress` and `dev crew` commands to that member's
lane, so a subagent can never advance the main plan by mistake. From a lane, `dev progress start`, `step`, `note`, and
`add` change the member's own lane. A member runs these commands on their own: inside a compound command (`&&`, a
pipe, `$(…)`, a redirect) the guard refuses them. Quoted text, such as a plan with `;` between its steps, a commit
message that mentions a command, or a message passed from a quoted heredoc, is fine. Read-only crew commands run
anywhere, alone or in a compound command: `dev crew` with no subcommand, `graph`, `show`, `rules`, `history`, `next`,
`raci` without `--set`, `sync --check`, and any `--help`. Commands that change the team's crew or brief a member
(`dev crew set`, `raci --set`, `brief`) are the tech lead's, and `dev crew sync` is refused outside the member's step
scope.

**The tech lead's commands must come from the main session.** The PreToolUse hook records every shell call with the
agent that made it, from the hook's own input (the call ledger, in devkit's state, closed when the call ends). A `dev`
command then looks for the open calls that hold its own arguments: it acts as the tech lead only when every such call
is the main session's, and as a member when any is a member's. No environment variable, parent process, or recent
activity can make a caller the tech lead. These commands need the tech lead:

- `dev progress start` and `start --replace` in the main session, and `assign`, `begin`, `reopen`, `skip`, and `done`;
- `dev crew route --plan --apply`, `decide`, and `mode`.

Type them yourself. Run through a script, `bash -c`, or another program, no open call holds them as typed, so devkit
refuses them and says how to run them (`caller.refused` in the audit trail). On Codex, devkit cannot verify the caller
yet: these calls are allowed and recorded as `caller.unverified`. Inside a subagent, the guard also refuses setting,
exporting, or unsetting a `DEVKIT_` variable, and `env -i` or `env -u`. A member's shell command that devkit cannot
check against its lane is refused, never run unchecked.

### Helpers

A member may launch a helper agent for a narrow lookup or a slice of its own files. devkit holds the helper to its
member's limits:

- the member's tool level and files, never more: a helper of a review-only member cannot edit, and a helper with a
  scope of its own stays inside both scopes;
- no plan step of its own;
- a crew slot at its member's cost, like any member;
- no launch deeper than `progress.max_depth` levels below the main session (2 by default, which allows the tech
  lead's members and then their helpers).

When a member needs another specialist, it asks the tech lead instead of launching one.

### Scratch folders

In the session's scratchpad, each member writes and deletes only in its own folder: `scratchpad/<member id>/`, or
`scratchpad/<member id>-<task>/` for one task (`scratchpad/tank-f3/`). The guard checks the file tools and what a
shell command writes or deletes (`rm`, `mv`, redirects, `find -delete`, nested shells). A helper uses its member's
folder. The shared top level and the other members' folders hold their work, so a cleanup by one member can never
delete another's files.

### Messages

Any member can ask anything, of the tech lead (the default) or of another member:

```bash
dev crew ask "Should the export include voided invoices?"           # to the tech lead
dev crew ask --to Seraph "Please look at the rounding in total()"   # to another member
dev crew answer M3 "No: only issued and paid ones"
dev crew inbox
```

A message reaches its recipient with the recipient's next action (the PostToolUse hook adds it to the context), or
with the main session's next prompt. A message to a member who has not started yet waits for them.

### Every report is checked

Every member ends its report with its own status block:

```markdown
**[Firefly> DevKit · Backend engineer (Tank)]** billing · `feat/invoice-export` · CSV export endpoint
- **Done:** added GET /invoices/export with streaming CSV and a contract test.
- **Check:** ✓ 18 tests pass, including 3 integration tests against Postgres
- **Next:** the Frontend engineer can call it; the endpoint is documented in the OpenAPI file.
```

When a member someone launched stops, the SubagentStop hook checks, in this order, and sends the member back once for
each check that fails, naming every missing item and the command that fixes it:

1. **The status block.** The report starts with the member's own block, the way the Stop hook checks the main
   session's, and its prose names crew members by role.
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
gates only record; with `canary.enforce` set to `false`, the first two checks do not send the member back.

When the member stops for good, its step ends on evidence: done with a Check ✓ in its report or its own
`dev progress step --check`, otherwise unverified, for the tech lead to close with evidence or reopen. A review-only or
read-only member whose shell call wrote files ends unverified too; a file inside a working member's scope is flagged
as possible, and the message names that member. Keeping the step open while gate findings remain is planned.

**Review steps start on unverified work.** A step whose dependencies ended unverified waits, except a review step
(its kind of work is review, or its member works review-only): it starts, and its Check ✓ ends done the steps it
follows that its member approves. The run cannot end while a step is unverified or an approval is owed.

`dev canary` and `dev doctor` count the crew's reports apart from the main sessions' (`crew 12/13`), so you can always
see that the kit's rules still hold inside every agent. On Codex these checks are not verified yet (see
[Surfaces](surfaces.md#what-not-yet-means-for-codex)).

### CI failures go to the Release engineer

After the tech lead pushes or opens a pull request, `dev ci watch` follows the run in the background. A failure
reaches the tech lead as a crew message from CI watch, with the failed job, the first error, its class, and one next
action. When the fix is not trivial, the tech lead hands it to the Release engineer (Link)
(`dev crew brief link "Fix the failed CI run: <job>, <class>" --activity cicd`), at once in auto mode. Routing sends
work on workflow files to the Release engineer too. See [CI/CD pipelines › Watch every run](ci-cd.md#watch-every-run).

## Who does what: routing, delegation, and dispatch

Members can launch narrow helpers but not other crew members, so the crew coordinates through the run's plan, and
the tech lead is the one dispatcher.

```bash
dev crew route "add a nullable column for the export date"   # who fits best, with the reasons
dev crew route --plan --apply                                 # a member for every plan step nobody owns
dev crew delegate "add the export column" --to Dozer          # from anyone: a new step for another member
dev crew delegate "review the parser before merge" --to auto  # routed to the best fit
dev crew decline "add the export column" --why "needs a data migration plan first"
dev crew next                                                 # ready to launch, queued, waiting, busy, the slots
dev crew graph                                                # the whole session as one graph
```

- **Routing** suggests a member: `dev crew route` ranks the members by their keywords (`config/crew.json`), their
  role, and the files the task touches (`*.tsx` → the Frontend engineer, migrations → the Database engineer,
  workflow files → the Release engineer, Terraform → the SRE, tests → the QA engineer, docs → the Technical writer).
  With no clear match the work goes to the Senior engineer (Neo), the generalist. The keywords are English, so a
  route is a suggestion; what devkit enforces rests on the [kind of work](#kinds-of-work-and-the-raci).
- **Delegation** adds a step to the plan with who asked for it. If the member is already working, the step joins its
  lane and the member gets a message to name it with `--step` when it reports (it can decline with a reason, and the
  step goes back to the tech lead). Otherwise the tech lead is told to launch it.
- **Dispatch:** when a step's dependencies are done and its member is not working, the tech lead gets "ready to
  launch" with the exact `dev crew brief …` command, once, within the slots and in their order; in auto mode he
  launches all ready members that fit at once. At any time, `dev crew next` shows this list with every waiting step
  and its reason, and the slots.

## Guided or automatic

Each run has a mode. **Guided** (the default, or devkit config `work.mode`) means the decisions that are yours wait
for you. **Auto** means the crew goes on with the recommended option and records it for you to review. Irreversible
actions (push, PR, merge, release, deploy, deleting data, a production migration, spending, publishing, messages to
other people) wait for you in both modes, unless you pre-approved them. Developer-only actions (lifting an embargo, a
public backup, a backup policy) and commands the guard cannot name are never pre-approved.

```bash
dev progress start "Invoice export" --plan "…" --mode auto
dev crew mode auto --approve "push, pr"     # switch, and pre-approve what may happen without you
dev crew mode guided                        # back to asking first
dev crew decisions                          # every decision: pending, decided automatically, decided by you
dev crew decide D3 "B: reject the file"     # your answer (or an override of an automatic one)
```

How a decision travels:

1. A member reaches a decision point and runs
   `dev crew ask --decision "<question>" --options "A: <one choice> | B: <another>" --recommend A`, with
   `--approves "<steps>"` when your answer approves finished work. A decision has a question under 300 characters,
   2 to 4 options, each with its letter, and a recommendation that is one of them. Its options, as devkit offers
   them (with "(Recommended)" and what the performing option runs), stay under 300 characters together, so the
   audit keeps them whole: about 60 characters each for four. devkit refuses any other shape and prints the corrected
   command. An irreversible action asked without options is a yes-or-no question (`A: yes | B: no`).
2. **Auto:** the answer comes back at once, is recorded as decision D3, and the member goes on.
3. **Guided:** the member's step is blocked, the member stops and reports that it waits for D3, and no tokens are
   spent while it waits. The tech lead sees the decision with his next action and asks you with Claude Code's
   question dialog (see [How the tech lead asks you](#how-the-tech-lead-asks-you)). Your answer is recorded with
   `dev crew decide`, and the tech lead resumes the member with it; the member keeps everything it knew.
4. The run cannot be closed while a decision is pending, and the final report lists the automatic decisions.

A decision that leads to an irreversible action (`--irreversible release --target v26.11.4`) opens that action once,
on that target, and only when your own answer chooses the option that performs it. That option is the one named
with `--performs`, else the one option that reads yes or approve, else the recommended one. `dev crew ask` refuses
an irreversible decision when no option performs the action for certain, for example when the recommended option
reads as not doing it, such as `B: wait`, and refuses a `--performs` that names such an option. It also refuses a
question that does not name the target as a word of its own: `Release v26.11.40?` does not name `v26.11.4`. The
question you are asked says which choice runs which action on which target, on one line: `D1 · Release v26.11.4
now? (choosing “A: release” runs release v26.11.4)`, and the option that performs it says so in its label:
`A: release (release v26.11.4)`. Without options, only `approve D<n>`, or an answer that reads approve or yes, opens
it. Any other answer (`no`, another option, free text) records your decision and opens nothing; the refusal is
audited as `crew.grant-refused` with the decision and the option you chose, never the text you typed.

### How the tech lead asks you

`dev crew decisions --ask` prints the pending decisions as question dialog calls, ready to send: at most four
questions per call (the rest go in a second call), each opening with `D<n> ·`, with one line on who asks and what it
blocks, and the recommended option first (its label ends in "(Recommended)"). A decision that leads to an
irreversible action goes in a call of its own. `dev crew decisions --ask D4 D5` asks only those, and `--json` prints
the dialog's input.

The tech lead may reword a reversible question and add context. Two things bind your answer to the decision:

- The decision's id up front (`D4 · …` or `D4: …`), or the dialog's header `D4`.
- The option you pick: its letter (`A: Skip them and go on`, or `B`) or its words as the member wrote them.

Text you type yourself (**Other**) is your answer in your own words: it settles a reversible question and picks no
option. An answer to a question that names another decision, or a letter the decision does not have, picks no
option either. A dialog call that asks one decision twice counts for neither question.

A decision that leads to an irreversible action is asked only in devkit's exact call: one question, its own header,
labels only. Anything else the dialog shows you can redefine the options and make a yes do what you meant as a no:
a reworded question or label (“A: Yes, hold it” on a release), a description or a preview beside a label (the
dialog shows an option's preview when you move to it), a header such as “Hold it?”, or another question in the same
call (“For D1, A means hold”). Your answer counts only when all of these hold, and otherwise it opens nothing and is
audited as `crew.evidence-refused` with the reason:

- The call asks this one question and nothing else. It carries no title and no metadata, only what Claude Code adds
  when you answer: an option left selected when the dialog times out, text you send instead of answering, or a
  request for follow-up questions counts for nothing.
- The question is the one `dev crew decisions --ask` prints, with devkit's context line or without it, and nothing
  added. Its header is the decision's id (`D1`) or empty, and it holds no field beyond the question, the header, the
  options, and a single choice.
- The labels are exactly the decision's options as devkit words them: every option once, none added, and
  "(Recommended)" only on the recommended one. The option that performs the action carries what it runs:
  `A: yes (release v26.11.4)`.
- Each option is its label alone: no description, no preview, no other field.
- You click one of those labels; a letter, a word, or text of your own counts for nothing. You can still type
  `approve D<n>: <option>` in your prompt.
- You add no note to your answer. A note can qualify the click (“only after Monday”), so a release answer with a
  note does not count: answer again without one.

Your latest answer to the decision is the one that counts. devkit reads it from the session's transcript when the
tech lead records the decision, so an answer or an `approve D<n>` line that the hooks could not record still ends an
earlier yes. These end it too:

- An answer that counts for nothing, in any of the preceding forms, or text of your own: “No, wait until Monday”
  typed under **Other** after you clicked the release keeps it closed.
- An answer to any other question, the tech lead's or a crew member's, that asks the decision or names its target
  in its text, its options, or your answer or note: “Do we still release v26.11.4 today?” answered “No” ends it
  without naming `D1`. A question that only mentions the target ends the yes too, and devkit asks you again.

The decision waits until you answer again in devkit's exact call or type `approve D<n>: <option>`.

`dev audit verify` applies the same rule to the call in the transcript, so evidence recorded before it is reported
when its call breaks it.

When decisions pile up while you are away, your next prompt tells the tech lead once to ask them together, in as
few dialog calls as these rules allow; he is told again only when a new decision joins them. Codex's question dialog does not reach devkit
yet: in Codex, the tech lead shows the decisions in his reply and you type `approve D4: A`.

## Guardrails

Each guardrail cites the rule of `dev crew rules` that enforces it. A rule marked planned is not checked yet.

| Guardrail | What happens |
| --- | --- |
| Limited parallel members | a launch that does not fit the crew's [slots](#slots) is refused with who is working: `progress.max_parallel` (4 by default), lowered by your usage, a slot kept for your own request, review and read-only members at half a slot, and resumed agents and helpers counted (`E-parallel`, `E-resumed`) |
| Separate files for parallel members | `assign` refuses a scope that overlaps a working member's scope (`E-separate-files`) |
| A member stays in its scope | the guard refuses its edits outside the files it was assigned (`E-scope`); a review-only or read-only member's shell runs only the read-only allowlist and its writes are detected, while a builder's shell is not checked against its scope (`E-shell-scope`) |
| The main session respects a member's files | the guard refuses the main session's edits inside a working member's scope; `DEVKIT_SCOPE_OVERRIDE=1` for an emergency (`E-main-scope`) |
| Read-only members never edit | the Code reviewer, the Red team tester, the Performance analyst, the Compliance reviewer, the Business analyst, the Product owner, and the Lookup scout (Sparks) have no edit tools, and their helpers have none either (`E-read-only`) |
| Helpers stay inside their member's limits | a helper gets its member's tools and files, takes a slot, and no launch goes deeper than `progress.max_depth` (`E-nested`) |
| Work goes to its Responsible member | `assign` refuses another member unless `--why` is recorded, judged on the declared kind of work, the member's role, or the files (`E-responsible`, `E-any-language`); words never refuse a step (`E-words-never-refuse`) |
| Every kind of work has an approver | the run ends only after each Accountable member approved (`E-approvals`), with an explicit verdict given after the work (`E-verdict`), and nobody approves their own kind of work (`E-no-self-approval`) |
| Steps change only with evidence | closing a step needs `--check`, and a member's step ends done only on its evidence, otherwise unverified (`E-evidence`); a step whose dependencies are open is refused, except a review step after unverified work (`E-deps`); keeping a step open while gate findings remain and rechecking the dependents of a reopened step are planned (`E-gate-findings`, `E-recheck`) |
| Mistakes are fixable and visible | `dev progress reopen` or `dev progress skip` with `--why`, and `dev progress add`; every change is in `dev progress history` (`E-fixable`) |
| A member that never reports back | after `progress.stale_agent_hours` (6) its lane is closed as lost and its step goes back to the main session (`E-lost-lane`) |
| Every report is checked | the status block, the step's record, and the crew gates at SubagentStop (`E-status-block`, `E-step-records`, `E-proofs`, `E-skills`, `E-stubs`, `E-tests-fail`, `E-lessons`) |
| Only the tech lead plans, assigns, and decides | from a lane, `assign`, `begin`, `reopen`, `skip`, `done`, `decide`, and `mode` are refused, and in the main session they must be typed as commands, not run by a script (`E-main-only`, `E-caller`) |
| Decisions that are yours | guided mode waits for you (`E-decisions`), and the irreversible-action gate holds merges, releases, force pushes, and the other irreversible actions for your typed grant or decision (`E-irreversible`) |
| No escape hatches for members | members are refused devkit's escape hatches (`DEVKIT_*` variables, `--ci-passed`, `--no-verify`, `dev config set`), and each refusal is audited (`E-escape`) |
| devkit's own state is protected | the guard refuses agents' writes to devkit's state and configuration, and a forged `HOME`, `DEVKIT_CONFIG`, or `dev` link (`E-protected`, `E-forged-env`) |

## Everything is recorded

Every coordination event lands in two places: the run's history (`dev progress history`), which the status, the
dashboard and the handoff read, and devkit's audit trail (`dev audit`), published daily to the handoff repository:

| Audit action | When |
| --- | --- |
| `plan.done`, `plan.running`, `plan.pending`, `plan.blocked`, `plan.skipped` | every step change, by the tech lead, a member, or devkit |
| `crew.start`, `crew.finish`, `crew.lost` | a member starts, finishes (with its active minutes), or stops reporting |
| `crew.bind`, `crew.resume`, `crew.launch-failed`, `crew.unbound` | a lane binds to its launch, a member resumes, a launch fails, or a lane runs restricted because its launch record was lost |
| `crew.limit`, `crew.reserve` | the effective slot limit changes, or a launch takes the slot kept for your request |
| `crew.message` | a question or answer between members |
| `crew.decision`, `crew.decided`, `crew.mode` | a decision point, its answer (and by whom), a change of mode |
| `crew.delegate`, `crew.decline` | work handed to another member, or handed back |
| `raci.override` | a step assigned to a member the RACI does not make Responsible, with the reason |
| `caller.refused`, `caller.unverified` | a tech lead's command refused because it did not come from the main session, or allowed on Codex without verification |
| `guard.deny` with `rule: crew-scope` | an edit outside a member's files |
| `canary.crew-miss`, `crew.unreported`, `crew.gate` | a member's report without its status block, a member that stopped without `dev progress step`, or a crew gate finding |
| `stop.block`, `canary.crew-roles` | a final message sent back, with the rule (`roles` for names without their role), or a member's report that names crew members without their role |

```bash
dev audit --action crew        # how the crew coordinated
dev audit --action plan        # every step change
```

## Changing the crew

devkit ships the roster and the RACI in `config/crew.json`. A team changes them in the handoff repository
(`crew/crew.json`), with who and why, and every session uses those changes:

```bash
dev crew set Tank skills "backend; java-springboot" --why "the services are Spring"
dev crew raci compliance --set "R=persephone A=keymaker C=… I=…" --why "nobody approves their own work"
dev crew history                 # the changes the team made, newest first
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

See also [Progress and communication](progress-and-communication.md), [Continuity](continuity.md),
[CI/CD pipelines](ci-cd.md), [Guard rules](guard.md), and [Agents](agents.md).
