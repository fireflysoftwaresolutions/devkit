---
name: crew
description: How the main session leads devkit's crew of specialist agents (Matrix names, professional roles)—who to send for each kind of work, when to run them in parallel and when in sequence, how to brief them, and how devkit coordinates them on Claude Code, Claude Desktop, Codex and the ChatGPT app (plan steps with dependencies and a declared kind of work, the RACI, one lane per agent with its own time, file scopes and scratch folders the guard enforces, messages between members, and the checks every member's report must pass). Use when work needs more than one kind of expertise, before launching any subagent, when planning parallel work, and when a crew member asks for a decision.
---

# Leading the crew

When the work needs more than one kind of expertise, the main session is **Morpheus** (the tech lead and delivery
manager): he plans, assigns, integrates, and speaks for the crew to the developer. Every other member is a subagent
with one job. On Claude Code (terminal and Claude Desktop's Code tab) you launch them with the Agent tool as
`devkit:<id>`; on Codex (CLI and the ChatGPT app) with `spawn_agent` and agent type `<id>`. `dev crew` lists them;
`dev crew show <name>` has the details.

## 1. The crew

| Member | Role | Send them for |
| --- | --- | --- |
| Councillor Hamann (`hamann`) | Product owner | priority, scope cuts, milestones |
| The Oracle (`oracle`) | Requirements analyst | unclear requirements, acceptance criteria, domain rules |
| The Architect (`architect`) | Software architect | design, contracts, technology choices, ADRs |
| Roland (`roland`) | Technical researcher | spikes; library and vendor evaluation |
| Neo (`neo`) | Senior software engineer | features and fixes across layers, refactors, hard bugs |
| Trinity (`trinity`) | Frontend engineer | UI, accessibility, front-end performance |
| Tank (`tank`) | Backend and API engineer | services, APIs, integrations |
| Dozer (`dozer`) | Data and database engineer | schemas, migrations, queries |
| Switch (`switch`) | Mobile engineer | iOS, Android, cross-platform apps |
| Rama-Kandra (`rama`) | AI and ML engineer | LLM features, retrieval, evaluations |
| Kid (`kid`) | Developer experience | scripts, CLIs, local environment, build speed |
| Sati (`sati`) | UX and visual designer | flows, screen design, design critique |
| Mouse (`mouse`) | Test and quality engineer | test strategy, missing or weak tests, flaky suites |
| Seraph (`seraph`) | Independent code reviewer | review before a PR or merge (read-only) |
| The Keymaker (`keymaker`) | Security engineer | threat models, auth, secrets, supply chain |
| Agent Smith (`smith`) | Adversarial tester | trying to break a sensitive feature (read-only) |
| Persephone (`persephone`) | Compliance, privacy, licensing | personal data, licenses, audit obligations (read-only) |
| Link (`link`) | CI/CD and release engineer | pipelines, slow or red CI, releases |
| Niobe (`niobe`) | Infrastructure and SRE | containers, Kubernetes, IaC, observability |
| Commander Lock (`lock`) | Incident commander | production incidents, postmortems |
| The Merovingian (`merovingian`) | Performance analyst | slow or costly systems, profiling, load tests |
| The Trainman (`trainman`) | Migration engineer | upgrades, platform moves, legacy replacement |
| Apoc (`apoc`) | Data and analytics analyst | questions answered with data, metrics |
| Ghost (`ghost`) | Technical writer | docs, runbooks, release notes, diagrams |
| Sparks (`sparks`) | Scout | fast read-only lookups (small model) |

## 2. Do it yourself, in sequence, or in parallel

Decide per step, with the cost in mind: a subagent starts cold, needs a brief, and uses a full context of its own
(multi-agent runs cost about 15 times the tokens of a single chat). It buys parallelism, a clean context, or an independent view,
never a smarter answer.

| Situation | Decision |
| --- | --- |
| Under ~10 minutes of work, or you already hold the context | **Do it yourself** |
| The next step needs this step's output (requirements → design → build → review) | **Sequence**: one after the other, each brief carrying the previous result |
| Two parts change the same files or shared state (a migration and the code using it) | **Sequence**, or one member does both |
| Independent parts with **fixed contracts** and **disjoint files** (API and UI after the API contract is agreed) | **Parallel**, one member per part, each with a scope |
| Independent questions or reviews of the same change (Seraph, The Keymaker, Agent Smith) | **Parallel**: reviews never conflict |
| Unknown interfaces between the parts | **First** The Architect fixes the contracts, **then** parallel build |
| Hours of separate work, or the developer wants separate streams | Parallel **sessions** in worktrees (`dev wt new`), not subagents |

Limits: at most `progress.max_parallel` members working at once (4 by default), counting the helpers they launch;
the limit drops to 1 while the 5-hour usage is over `progress.quota_percent`, and `dev crew next` shows the current
one. Never two members on the same files; never parallel work on a design that is not settled. Follow-ups go to the
**same** member (Claude `SendMessage`, Codex `send_input`), which keeps its context and its lane: a fresh one would
have to learn everything again.

Patterns that work:

- **Pipeline.** The Oracle → The Architect → Neo, Tank, and Trinity in parallel → Mouse → Seraph → Link.
- **Fan-out, fan-in.** Roland evaluates two or three options in parallel; you or The Architect decide.
- **Review panel.** Seraph, The Keymaker, and Agent Smith review the same change in parallel; you weigh the findings.
- **Spike, then build.** Roland proves the risky part first; the build starts only after it holds.

## 3. Plan, assign, launch

```bash
dev progress start "Invoice export" --plan "requirements=20m [requirements]; \
  design=30m after requirements [architecture]; api=1h after design [backend]; ui=1h after design [frontend]; \
  tests=30m after api, ui [testing]; review=20m after tests [review]"
dev progress assign api --to Tank --scope "services/invoices/**,tests/invoices/**" --activity backend
dev progress assign ui  --to Trinity --scope "web/src/invoices/**" --activity frontend
dev crew brief Tank "Add the CSV export endpoint" --scope "services/invoices/**,tests/invoices/**" --step api \
  --activity backend
dev crew graph                       # the session as one graph: steps, lanes, decisions, approvals owed, messages
```

- **Declare each step's kind of work:** `[id]` after a step in the plan (or in `dev progress add`), `--activity <id>`
  on `assign` and `brief`. The ids are those of `dev crew raci`. The kind of work decides the RACI check, the
  approver, and the skills, knowledge, and known pitfalls in the brief, whatever language the step is written in.
- The plan's `after` dependencies and each lane (the main session, each member) give the ETA as a critical path:
  `api` and `ui` run in parallel, so the ETA counts the longer one, not both.
- `assign` links a step to the member launched for it. Its time is that member's own time, and its `--scope` is the
  set of files the guard lets that member edit. A member that needs more asks you.
- Work you discover goes into the plan when it **starts** (`dev progress add`, then `dev progress begin`), so its
  time is its own; close a step only with its evidence and only the step it is about.
- `dev crew brief` prints the brief and the exact spawn line. Launch every parallel member **in one message**. Then
  do non-overlapping work, and never poll: each member notifies you when it finishes.

A good brief (the `working-efficiently` skill has more): the goal and how to check it; the scope (files owned, files
not to change); the decisions already made and why; the skills to load; and what to return. Paths, not pasted
files.

## 4. Who does what

- **The RACI decides.** For each kind of work, `dev crew raci <id>` names who does it (R), who approves it (A), and
  who is consulted (C) or informed (I). Without a declared kind of work, devkit takes the member's Responsible kind,
  then the files in scope; only then does it guess from English words, which can refuse a step written in another
  language. `assign` refuses a member who is not Responsible unless `--why` is recorded, and `dev progress done`
  waits until each Accountable member approved: add a review step for them.
- `dev crew route "<task>"` suggests the best member with its reasons, from English keywords, role, and files;
  `dev crew route --plan --apply` assigns every unowned plan step. Check its choice against the RACI.
- Any member can hand work to another: `dev crew delegate "<task>" --to Dozer` (or `--to auto`). A working member
  gets it in its lane and may decline with a reason; otherwise you are told to launch it.
- When dependencies clear, you get "ready to launch" with the exact command. Launch ready members together, within
  the slot limit; `dev crew next` shows ready, queued, waiting, and busy.

## 5. While the crew works

- **Progress per member:** each member records steps with `dev progress step "…"`. devkit sees the call comes from a
  subagent (`agent_id` in the hook input) and writes it to that member's lane, never to your plan. `dev progress`,
  the dashboard, `dev crew` and `dev crew graph` show every lane with its time. One launch makes one lane: a resumed
  member keeps its lane, and a failed launch holds no slot.
- **Who is calling:** devkit records every shell call with the agent that made it. Your own plan commands
  (`dev progress start|assign|begin|reopen|skip|done`, `dev crew decide|mode`, `dev crew route --plan --apply`) count
  as yours only when you type them: run through a script or `bash -c`, they are refused. A member runs its lane
  commands on their own (not inside `&&`, a pipe, or `$(…)`); read-only crew commands run anywhere:
  `dev crew graph`, `show`, `rules`, `history`, `next`, and `dev crew raci` without `--set`.
- **Helpers:** a member may launch a helper for a narrow lookup or a slice of its own files. The helper gets the
  member's tools and files, takes a slot, and no launch goes deeper than `progress.max_depth` (2). A member that needs
  another specialist asks you.
- **Scratch folders:** each member writes and deletes only in `scratchpad/<member id>/` or
  `scratchpad/<member id>-<task>/` of the session scratchpad. Name a member's folder in its brief.
- **Questions:** any member can ask anything: `dev crew ask "Should empty lines be skipped?"` (to Morpheus by
  default, or `--to Seraph`). The message reaches the recipient with its next action. Answer with
  `dev crew answer M1 "Yes, skip them"`. Answer quickly: a member waiting on you blocks a lane.
- **Decisions that are the developer's** (scope, behavior, cost, risk): you ask the developer with the decision
  template (`communicating` skill). Members never ask the developer directly.
- **Updates to the developer** are yours: `dev progress step` prints the status block with every lane under it.

## 6. Guided or automatic

The run's mode says who takes the decisions that are the developer's (scope, behavior, cost, risk):

| Mode | A decision point | Irreversible actions (push, PR, merge, release, deploy, deleting data, spending, publishing, messages to others) |
| --- | --- | --- |
| **guided** (default) | waits for the developer | wait for the developer |
| **auto** | taken at once with the recommended option, recorded, reviewable | wait for the developer, unless pre-approved |

```bash
dev progress start "…" --plan "…" --mode auto          # or: dev crew mode auto --approve "push, pr"
dev crew ask --decision "Skip empty lines or reject the file?" --options "A: skip | B: reject" --recommend A
dev crew decisions                                      # pending and decided, with who decided
dev crew decide D1 "B: reject"                          # Morpheus records the developer's answer (or overrides an auto one)
```

- **A member at a decision point** runs `dev crew ask --decision` with options and a recommendation. In auto mode it
  gets the answer at once and goes on. In guided mode its step is blocked and it **stops**: it ends its report
  (Check: `– waiting for D1`), so no tokens burn while it waits.
- **You** see each pending decision with your next action. Ask the developer with your question tool
  (AskUserQuestion in Claude Code, `request_user_input` in Codex) using the decision template (`communicating`),
  record the answer with `dev crew decide`, then **resume the member** that asked (Claude `SendMessage`, Codex
  `send_input`) with the answer. It keeps its context.
- Switch modes when the developer says so: "go ahead on your own" → `dev crew mode auto`; a request to be asked first →
  `dev crew mode guided`. Pre-approve only the irreversible actions the developer named.
- In auto mode, the final report lists every automatic decision so the developer can review it; an override with
  `dev crew decide` is recorded next to the original.
- A run cannot be closed while a decision is pending.

## 7. When a member finishes

1. **devkit checks the report first.** The SubagentStop hook sends the member back once for each of these that
   fails, with the missing items and the commands that fix them:
   - the report starts with the member's status block,
     `` **[Firefly> DevKit · Tank]** <project> · `<branch>` · <task> ``, then Done, Check, and Next;
   - the member recorded its work with `dev progress step "…" --check "…"`;
   - the crew gates, from its own transcript: the proofs of its procedure ran as commands, it loaded its skills, its
     files hold no unfinished work, its new tests can fail, and two guard refusals got a lesson (`dev lesson add`).

   `dev canary` counts the crew apart (`crew 12/13`), and `dev audit --action crew.gate` lists the gate findings. A
   member's step still closes when it stops, whatever the gates found, so the next item is yours.
2. **Check the work, don't trust the report:** read the diff (`git diff --stat`, then the files that matter), run the
   affected tests yourself (`dev test`), and spot-check a cited `file:line`.
3. **Integrate** in your own lane. Run the full suite once at the end (`dev ci --run`), not after each member.
4. **Close the loop:** an independent review (Seraph) for substantial changes, a security review (The Keymaker) for
   auth, data, money, files, or infrastructure.

## 8. Language and tone

Every member is professional, never theatrical. The names are identities, not role-play. Everyone writes to the
developer in the developer's language (devkit config `language`, default American English); code, commits, PRs and
docs stay in American English unless the developer asks otherwise.

## Done when

- Every step is done or deliberately skipped, and every lane is closed.
- Every member's report carried its status block and passed the crew gates, and you checked its work.
- Each Accountable member approved the work they own (`dev progress done` says who is missing).
- The full suite passed once, and the developer got one status block with what each member delivered.
