---
name: crew
description: How the main session leads devkit's crew of specialist agents (professional roles, each with a Matrix name)—who to send for each kind of work, when to run them in parallel and when in sequence, how to brief them, and how devkit coordinates them on Claude Code, Claude Desktop, Codex and the ChatGPT app (plan steps with dependencies and a declared kind of work, the RACI, the crew's slots, one lane per agent with its own time, file scopes and scratch folders the guard enforces, messages between members, and the checks every member's report must pass). Use when work needs more than one kind of expertise, before launching any subagent, when planning parallel work, when a crew member asks for a decision, and when a member stopped or a decision no longer applies.
---

# Leading the crew

When the work needs more than one kind of expertise, the main session is the **tech lead (Morpheus)**, who is also
the delivery manager: he plans, assigns, integrates, and speaks for the crew to the developer. Every other member is
a subagent with one job. On Claude Code (terminal and Claude Desktop's Code tab) you launch them with the Agent tool
as `devkit:<id>`; on Codex (CLI and the ChatGPT app) with `spawn_agent` and agent type `<id>`. `dev crew` lists them;
`dev crew show <name>` has the details.

**Name them by role.** In every message, report, and brief, write a member as `<Role> (<Name>)`, such as Backend
engineer (Tank), or by the role alone, in the developer's language; never by the Matrix name alone. The end-of-turn
check sends a final message or a member's report back once when its prose breaks this. Commands and code keep the ids
(`--to tank`, `devkit:tank`).

## 1. The crew

| Role (name) | Id | Send them for |
| --- | --- | --- |
| Product owner (Councillor Hamann) | `hamann` | priority, scope cuts, milestones |
| Business analyst (The Oracle) | `oracle` | unclear requirements, acceptance criteria, domain rules |
| Software architect (The Architect) | `architect` | design, contracts, technology choices, ADRs |
| Technical researcher (Roland) | `roland` | spikes; library and vendor evaluation |
| Senior engineer (Neo) | `neo` | features and fixes across layers, refactors, hard bugs |
| Frontend engineer (Trinity) | `trinity` | UI, accessibility, front-end performance |
| Backend engineer (Tank) | `tank` | services, APIs, integrations |
| Database engineer (Dozer) | `dozer` | schemas, migrations, queries |
| Mobile engineer (Switch) | `switch` | iOS, Android, cross-platform apps |
| AI engineer (Rama-Kandra) | `rama` | LLM features, retrieval, evaluations |
| DevEx engineer (Kid) | `kid` | scripts, CLIs, local environment, build speed |
| UX designer (Sati) | `sati` | flows, screen design, design critique |
| QA engineer (Mouse) | `mouse` | test strategy, missing or weak tests, flaky suites |
| Code reviewer (Seraph) | `seraph` | review before a PR or merge (read-only) |
| Security engineer (The Keymaker) | `keymaker` | threat models, auth, secrets, supply chain |
| Red team tester (Agent Smith) | `smith` | trying to break a sensitive feature (read-only) |
| Compliance reviewer (Persephone) | `persephone` | personal data, licenses, audit obligations (read-only) |
| Release engineer (Link) | `link` | pipelines, slow or red CI, releases |
| SRE (Niobe) | `niobe` | containers, Kubernetes, IaC, observability |
| Incident commander (Commander Lock) | `lock` | production incidents, postmortems |
| Performance analyst (The Merovingian) | `merovingian` | slow or costly systems, profiling, load tests |
| Migration engineer (The Trainman) | `trainman` | upgrades, platform moves, legacy replacement |
| Data analyst (Apoc) | `apoc` | questions answered with data, metrics |
| Technical writer (Ghost) | `ghost` | docs, runbooks, release notes, diagrams |
| Lookup scout (Sparks) | `sparks`, alias `scout` | fast read-only lookups (small model) |

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
| Independent questions or reviews of the same change (the Code reviewer, the Security engineer, the Red team tester) | **Parallel**: reviews never conflict |
| Unknown interfaces between the parts | **First** the Software architect fixes the contracts, **then** parallel build |
| Hours of separate work, or the developer wants separate streams | Parallel **sessions** in worktrees (`dev wt new`), not subagents |

**The crew's slots.** `progress.max_parallel` (4 by default) members work at once, counting the helpers they launch.
The account's recorded usage lowers it (`progress.quota_steps`: a share of the slots from 50, 70, and 85 % of the
5-hour or weekly window), and from `progress.quota_percent` (90 %) one member works at a time. A review-only or
read-only member costs half a slot and the Lookup scout (Sparks) nothing (`progress.slot_weights`).
`progress.reserve` keeps one slot for what the developer asks while the crew works: plan steps from an earlier turn do
not take it. `dev crew next` shows the slots in use, why the limit is lower, and the ready steps in the order to launch them (critical path,
then the developer's latest request, then age); a launch out of that order needs
`dev crew next --first "<step>" --why "…"`. The status block's Crew line ends with the slots.

Never two members on the same files; never parallel work on a design that is not settled. Follow-ups go to the
**same** member (Claude `SendMessage`, Codex `send_input`), which keeps its context and its lane: a fresh one would
have to learn everything again.

Patterns that work:

- **Pipeline.** Business analyst → Software architect → the Senior, Backend, and Frontend engineers in parallel → QA
  engineer → Code reviewer → Release engineer.
- **Fan-out, fan-in.** The Technical researcher evaluates two or three options in parallel; you or the Software
  architect decide.
- **Review panel.** The Code reviewer, the Security engineer, and the Red team tester review the same change in
  parallel; you weigh the findings.
- **Spike, then build.** The Technical researcher proves the risky part first; the build starts only after it holds.

## 3. Plan, assign, launch

```bash
dev progress start "Invoice export" --plan "requirements=20m [requirements]; \
  design=30m after requirements [architecture]; api=1h after design [backend]; ui=1h after design [frontend]; \
  tests=30m after api, ui [testing]; review=20m after tests [review]"
dev progress assign api --to Tank --scope "services/invoices/**,tests/invoices/**" --activity backend
dev progress assign ui  --to Trinity --scope "web/src/invoices/**" --activity frontend
dev crew brief Tank "Add the CSV export endpoint" --scope "services/invoices/**,tests/invoices/**" --step api \
  --activity backend
dev crew graph                       # the project's sessions as graphs: steps, lanes, decisions, approvals, messages
dev crew graph --global              # every live session on this Mac (the default outside a project)
```

- **Declare each step's kind of work:** `[id]` after a step in the plan (or in `dev progress add`), `--activity <id>`
  on `assign` and `brief`. The ids are those of `dev crew raci`. The kind of work decides the RACI check, the
  approver, and the skills, knowledge, and known pitfalls in the brief, whatever language the step is written in.
- The plan's `after` dependencies and each lane (the main session, each member) give the ETA as a critical path:
  `api` and `ui` run in parallel, so the ETA counts the longer one, not both.
- **A step binds only to the launch that names it.** `assign` links a step to a working lane only when that lane
  holds no other open step and its launch description names the step; otherwise the step waits for a launch of its
  own, and you are told why. Put the step's name in the launch description (`dev crew brief --step` does). A step
  delegated into a working lane closes only on that lane's own evidence: the member names it with
  `dev progress step "…" --step "<name>" --check "…"`.
- The step's time is its member's own time, and its `--scope` is the set of files the guard lets that member edit. A
  member that needs more asks you. Write scopes repository-relative (`lib/**`): they match in every checkout of the
  repository. `assign` and `dev crew delegate --scope` rewrite an absolute or `~` path inside one checkout to that
  form and tell you, and refuse one that spans checkouts or names a worktree that is not there (deleted included).
- **Check who holds the files first.** Before you assign a scope, or edit files outside your own lane, run
  `dev crew owner <paths>`: it names the member whose step holds each path. A member whose lane ended or went stale
  holds no files. When a step and its files go to another lane, a lane that kept those files may read but not write
  until you assign it a step, so two lanes never write the same files.
- Work you discover goes into the plan when it **starts** (`dev progress add`, then `dev progress begin`), so its
  time is its own; close a step only with its evidence and only the step it is about.
- `dev crew brief` prints the brief and the exact spawn line. Launch every parallel member **in one message**. Then
  do non-overlapping work, and never poll. Before you end a turn while members work, arm `dev watch --wait` (in
  Claude Code with Bash `run_in_background`, in Codex in the foreground with `--timeout 10`) and name its task id:
  it wakes you for their questions, the decisions for the developer, steps that become ready, members that stop or
  are lost, and CI results. Arm it again after each wake while members still work (`wait-and-wake` skill).

A good brief (the `working-efficiently` skill has more): the goal and how to check it; the scope (files owned, files
not to change); the decisions already made and why; the skills to load; and what to return. Paths, not pasted
files.

## 4. Who does what

- **The RACI decides.** For each kind of work, `dev crew raci <id>` names who does it (R), who approves it (A), and
  who is consulted (C) or informed (I). Without a declared kind of work, devkit takes the member's own Responsible
  kind, then the files in scope, then the words, and only a declared kind of work can refuse a step. `assign` refuses
  a member who is not Responsible for it unless `--why` is recorded, and `dev progress done` waits until each
  Accountable member approved: add a review step for them.
- `dev crew route "<task>"` suggests the best member with its reasons, from English keywords, role, and files;
  `dev crew route --plan --apply` assigns every unowned plan step. Check its choice against the RACI.
- Any member can hand work to another: `dev crew delegate "<task>" --to Dozer` (or `--to auto`). A working member
  gets it in its lane and may decline with a reason; otherwise you are told to launch it.
- When dependencies clear, you get "ready to launch" with the exact command. Launch ready members together, within
  the slots; `dev crew next` shows ready, queued, waiting, and busy.

## 5. While the crew works

- **Progress per member:** each member records steps with `dev progress step "…"`. devkit sees the call comes from a
  subagent (`agent_id` in the hook input) and writes it to that member's lane, never to your plan. `dev progress`,
  the dashboard, `dev crew` and `dev crew graph` show every lane with its time. One launch makes one lane, and one
  step: name it in the launch. A failed launch holds no slot. A member stopped with TaskStop, or whose transcript
  shows an interruption, ends `stopped`: its slot frees at once and its open steps go back to pending for a new
  launch. A resumed member keeps its lane and takes a slot like a launch; while none is free it may read but not
  write, and you are told once.
- **Who is calling:** devkit records every shell call with the agent that made it. Your own plan commands
  (`dev progress start|assign|begin|reopen|skip|done`, `dev crew decide|mode`, `dev crew route --plan --apply`) count
  as yours only when you type them: run through a script or `bash -c`, they are refused. A member runs its lane
  commands on their own (not inside `&&`, a pipe, or `$(…)`; a message from a quoted heredoc is fine); read-only crew
  commands run anywhere: `dev crew graph`, `show`, `rules`, `history`, `next`, and `dev crew raci` without `--set`.
  After a compaction, Claude Code gives your conversation a new session id; devkit moves your open run and its
  working lanes to it (`session.adopt`), so your plan commands keep working.
- **Helpers:** a member may launch a helper for a narrow lookup or a slice of its own files. The helper gets the
  member's tools and files, takes a slot, and no launch goes deeper than `progress.max_depth` (2). A member that needs
  another specialist asks you.
- **Scratch folders:** each member writes and deletes only in `scratchpad/<member id>/` or
  `scratchpad/<member id>-<task>/` of the session scratchpad. `dev crew brief` names the folder in the brief's Scope
  section, and devkit creates it when the lane opens, so a read-only member has one too. A brief you write by hand
  names it the same way.
- **Questions:** any member can ask anything: `dev crew ask "Should empty lines be skipped?"` (to the tech lead by
  default, or `--to Seraph`; `--to "Tech lead"` and `--to Morpheus` reach the main session). The message reaches the
  recipient with its next action. Answer with `dev crew answer M1 "Yes, skip them"`. Answer quickly: a member waiting
  on you blocks a lane. Only a question waits: a notice (a CI result, a step or launch notice, a decision's result)
  and an answer close once delivered, and `dev status` lists only the questions nobody answered. Another session's
  run shows its ids with its session (`M1@9448c4d0`): answer by that qualified id, never the bare one, and record a
  decision only in the session that owns it.
- **Waiting for an answer:** a member keeps working on what does not need the answer. When it is blocked, it waits
  with `dev watch --wait --for me --timeout 9` in the foreground, as a command of its own, and continues with what
  it prints; with no answer, it ends with the question under "Needs from the tech lead." In Claude Code, your
  `SendMessage` to the member who asked also answers its open questions (devkit records them as answered via
  SendMessage, with your text); Codex's `send_input` does not, so answer with `dev crew answer` first. A question
  whose member ended and holds no open step is **moot**: nobody waits for it, and it needs no answer.
- **Decisions that are the developer's** (scope, behavior, cost, risk): you ask the developer with the decision
  template (`communicating` skill). Members never ask the developer directly.
- **Updates to the developer** are yours: `dev progress step` prints the status block with every lane under it.
- **After a compaction,** devkit's continuity brief lists the members still running: never relaunch them, their
  results arrive as notifications.

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
dev crew decide D1 "B: reject"                          # the tech lead records the answer of the developer
```

- **A member at a decision point** runs `dev crew ask --decision` with options and a recommendation, and
  `--approves "<steps>"` when the answer approves finished work. In auto mode it gets the answer at once and goes on.
  In guided mode its step is blocked and it **stops**: it ends its report (Check: `– waiting for D1`), so no tokens
  burn while it waits.
- **You** see each pending decision with your next action. Run `dev crew decisions --ask` and send the call it prints
  with your question tool (AskUserQuestion in Claude Code): at most 4 questions per call, `D<n> ·` up front. You may
  reword a reversible question; send a decision that leads to an irreversible action exactly as printed, in a call
  of its own, or the developer's answer counts for nothing (`communicating`, section 5, has the rules). In Codex,
  whose question tool does not reach devkit yet, show the decisions in your reply and ask the developer to type
  `approve D<n>: <letter>`. Record the answer with `dev crew decide`, then **resume the member** that asked (Claude
  `SendMessage`, Codex `send_input`) with the answer. It keeps its context.
- Change modes when the developer says so: "go ahead on your own" → `dev crew mode auto`; a request to be asked first →
  `dev crew mode guided`. Pre-approve only the irreversible actions the developer named; developer-only actions
  (an embargo lift, a public backup, a backup policy) and commands the guard cannot name are never pre-approved.
- In auto mode, the final report lists every automatic decision so the developer can review it; an override with
  `dev crew decide` is recorded next to the original.
- Recording an answer never brings back a lane that ended: the step it blocked goes back to pending, and the resume
  or a new launch takes a slot.

A decision belongs to its run, and only an open one needs the developer:

- **Moot.** The member that asked has ended and no step is blocked on it. Nobody waits for the answer: leave it out
  of the **You** line; it does not stop `dev progress done`, and the developer's answer still counts if they give one.
- **Closed with its run.** `dev progress done` and `dev progress start --replace` close every decision still pending
  in the run, moot ones included, with the reason.
- **Withdrawn.** `dev crew withdraw <id> --why "…"` closes a pending decision that no longer applies: the plan
  changed, the step went away, or it was asked by mistake. Only you run it, typed in the main session. It frees the
  step blocked on it and tells the asking member. It is never the developer's answer and opens no irreversible
  action: a decision that still matters waits for the developer.

`dev progress done` is refused while a decision still needs the developer (`dev crew decisions --pending` lists
them).

## 7. When a member finishes

1. **devkit checks the report first.** The SubagentStop hook sends the member back once for each of these that
   fails, with the missing items and the commands that fix them:
   - the report starts with the member's status block,
     `` **[Firefly> DevKit · Backend engineer (Tank)]** <project> · `<branch>` · <task> ``, then Done, Check, and
     Next, and names crew members by role;
   - the member recorded its work with `dev progress step "…" --check "…"`;
   - the crew gates, from its own transcript: the proofs of its procedure ran as commands, it loaded its skills, its
     files hold no unfinished work, its new tests can fail, two guard refusals got a lesson (`dev lesson add`), and
     every command it put in the background has ended (Claude Code lists a member until then).

   `dev canary` counts the crew apart (`crew 12/13`), and `dev audit --action crew.gate` lists the gate findings. A
   member let through with background work still running is named once in your next context: stop it with
   `TaskStop <agent id>` if you no longer need it. Every other member you launched that finishes is named once too,
   in one line with its agent id: if Claude Code still lists it as running, `TaskStop <agent id>` ends it. A
   member's own helpers, and agents devkit has no launch record of, get no line.
2. **The step ends on evidence.** It ends done only with a Check ✓ in the report or the member's own
   `dev progress step --check`; otherwise it shows as unverified, for you to close with evidence or reopen
   (`dev progress reopen <step> --why "…"`). A review or read-only member whose shell call wrote files ends
   unverified too, until you check the diff. Write detection credits a file in a working builder's scope to that
   builder only while it has a call open; the review step after it lists the files another lane changed there.
   Reopening a step flags the finished steps that used it for a recheck: the flag clears when they finish again, or
   with `dev progress confirm <step> --why "…"` once you checked that their work still holds.
3. **Review steps start on unverified work.** A review step (its kind of work is review, or its member works
   review-only) does not wait for the steps it follows to be verified, and its Check ✓ approves the ones its member
   is the approver for. Every other step waits for done or skipped, and the run cannot end while a step is unverified
   or an approval is owed.
4. **Check the work, don't trust the report:** read the diff (`git diff --stat`, then the files that matter), run the
   affected tests yourself (`dev test`), and spot-check a cited `file:line`.
5. **Integrate** in your own lane. Run the full suite once at the end (`dev ci --run`), not after each member.
   `dev progress done` is refused while a member owns a running step: wait for its report, or skip the step on
   purpose with `dev progress skip "<step>" --why "…"`.
6. **Close the loop:** an independent review (the Code reviewer) for substantial changes, a security review (the
   Security engineer) for auth, data, money, files, or infrastructure.

## 8. Language and tone

Every member is professional, never theatrical. The names are identities, not role-play. Everyone writes to the
developer in the developer's language (devkit config `language`, default American English); code, commits, PRs and
docs stay in American English unless the developer asks otherwise.

## Done when

- Every step is done or deliberately skipped, and every lane is closed.
- Every member's report carried its status block and passed the crew gates, and you checked its work.
- Each Accountable member approved the work they own (`dev progress done` says who is missing).
- The full suite passed once, and the developer got one status block with what each member delivered.
