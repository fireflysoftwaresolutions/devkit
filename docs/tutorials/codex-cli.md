# Tutorial: your first task with Codex CLI

In this tutorial you open a new project in Codex, OpenAI's coding agent, in Terminal; check that devkit is active; and
give devkit's crew a real task: a small reading-list web page. On the way you learn to read what devkit shows you:
the status block, the progress updates, the moments it waits for you, the crew graph, and the dashboard. It takes
about 30 minutes, most of it while the crew works.

> **Partly verified live.** Codex CLI 0.167.0 sessions on November 9, 2026 exercised hooks, guard, skills, crew
> lifecycle, and compaction. The later context, routing, scratch, wait, and steering corrections have contract
> coverage; they still need a fresh-session check after installation. This complete tutorial flow and the terminal
> welcome remain unverified. See the [surface evidence matrix](../surfaces.md#evidence-matrix).

## What you need

- devkit installed on your Mac, with Codex chosen in the setup wizard and every line of `dev doctor` showing `✓`. If
  you have not installed it yet, follow
  [Your first install, step by step](../getting-started.md#your-first-install-step-by-step).
- A ChatGPT account with Codex, signed in. The setup wizard offers the sign-in; if you skipped it, run `codex` once and
  sign in through the browser window that opens.
- Terminal open. Press Command-Space, type `Terminal`, and press Return.

## Step 1: open a project

A project is a folder with your code in it. Create one for this tutorial and start Codex there:

```bash
dev new reading-list
dcd reading-list
codex
```

- `dev new reading-list` creates the folder `~/Development/_local/reading-list` and prints its path.
- `dcd reading-list` moves Terminal into that folder.
- `codex` starts Codex in it, with a box where you type. devkit marked every folder under `~/Development` as trusted
  in Codex, so Codex does not ask whether to trust this one.

For a project that already exists, `dcd <name>` and `codex`, or `dev codex <name>` in one step. To get a project from
GitHub first, run `dev clone <owner>/<repo>`.

## Step 2: check that devkit is active

Type this and press Return:

```text
What is this project, and what is its status?
```

The reply starts with a bold line that begins with `[Firefly> DevKit]`, followed by three bullets. For example:

```markdown
**[Firefly> DevKit]** reading-list · `main` · Project status
- **Done:** read the project: an empty git repository with no commits and no tasks yet.
- **Check:** – nothing to verify, a status question
- **Next:** tell me what to build; I plan it and bring in the crew.
```

That block is the **status block**. Every development reply should start with it. It checks the visible format,
not whether every hook ran. Use `dev canary` to inspect scored turns; an empty report is not a pass.
If the reply has no such line, go to
[When something goes wrong](#when-something-goes-wrong).

At the first prompt, devkit sends its welcome through the session-start hook. Its placement in the terminal UI is
not yet verified. For this new project the message reads:

```text
devkit · reading-list · new project
› No history yet: say what you want to build
· main · no commits yet
```

It is `dev status` in a few lines: what needs you, what runs, what is next, and where you are. [Concepts › The
session-start welcome](../concepts.md#the-session-start-welcome) explains each line.

## Step 3: give the crew a task

Type this request and press Return:

```text
Build a one-page web app in this folder where I can add books to a reading list and mark them as read. Keep the
list in the browser, add tests, and write a short README that explains how to open it.
```

What happens, and what you see:

1. **The tech lead routes the request.** Codex declares the kind of work when it plans, in any language. That
   declaration selects the responsible crew member: front-end work goes to the Frontend engineer (Trinity), with review by
   the Code reviewer (Seraph).
2. **Codex answers as the tech lead (Morpheus).** He posts a plan and an estimate. Work under ten minutes he does
   himself; this request gets steps for the crew.
3. **The crew starts.** The tech lead launches each member as a Codex agent role, for example `trinity`, in the
   order and slots shown by `dev crew next`. The brief lists required skills, and SubagentStart supplies skill
   context too. An over-limit launch is restricted after it starts because Codex has no pre-launch hook.
4. **Progress updates arrive** after each finished step, and every ten minutes or so while a long step runs.
5. **The final report** closes the run: what changed, the evidence, what is not done, and the next step.

For a registered CLI session in a managed checkout or worktree, temporary probes live under
`~/Development/.tmp/codex/<session>/scratchpad/<member>/`; the main session uses `main` in place of the member.
The launch names the exact folder. Read-only reviewers can run their own probes, but cannot write into other
members' folders or the lead's scratch. See [Codex scratch folders](../guard.md#codex-scratch-folders).

## Step 4: read what devkit shows you

### The status block, line by line

```markdown
**[Firefly> DevKit]** reading-list · `main` · Reading list
- **Done:** the page adds books and marks them as read; the list stays after a reload.
- **Check:** ✓ 6 tests pass in a headless browser
- **Progress:** 3 of 4 steps · now 4. review · 31m active of ~45m · ETA ~10m–15m
- **Crew:** Frontend engineer (Trinity) ✓ the page · QA engineer (Mouse) ✓ the tests · Code reviewer (Seraph) ▸ the review
- **Next:** the Code reviewer finishes the review, then I write the final report.
```

| Line | What it tells you |
| --- | --- |
| Header | The project, the branch, and the task in a few words |
| **Done** | What was done or found, in one sentence |
| **Check** | The proof. `✓` checked in this session, `⚠` partly or not checked, `✗` failed, `–` nothing to check |
| **Progress** | Required while a run is going: steps done of the total, the step running now, active time against the estimate, and when it is likely to finish. devkit sends a reply back once when it is missing |
| **Crew** | Only when crew members work: each one by its role and name, with `▸` working, `✓` done, or `✗` failed |
| **You** | Only when something waits for you: a decision, an approval, or an action on your Mac or account |
| **Next** | The next step, and who takes it |

macOS shows a notification when a run finishes or gets blocked.

### When devkit waits for you

- **A decision that is yours.** A crew member reaches a choice, for example whether finished books move to the bottom
  of the list. The member stops, and Codex asks you and gives the options and a recommendation. Answer in the
  conversation using the `approve D<n>: <letter>` line devkit asks for; the tech lead records it and the member goes
  on. Recovering a mid-turn request for continuity is not approval evidence.
- **An action that cannot be undone or leaves your Mac**, such as a push to GitHub, a pull request, a merge, a release,
  or deleting data. It waits for your OK, unless you approved it in advance.

With the recommended setup ("No prompts, full access"), Codex does not ask before each command. devkit's guard blocks
unsafe or wasteful commands and tells Codex the right way. Codex has no "ask" answer, so where Claude Code would ask
you, for example before a write to your Desktop, the guard refuses instead.

### The crew graph

Open a second Terminal window (Command-N) and run:

```bash
dev crew graph --latest
```

It draws the newest run on this Mac, with the tech lead, every step with its state and owner, what each step waits for, and the
decisions waiting for you. The steps carry `✓` done, `▸` running, `◇` ready to start, `○` waiting for another step,
`✗` blocked, `–` skipped, and `!` reopened to check again. The [Claude Code tutorial](claude-code.md#the-crew-graph)
shows a full example.

### The dashboard

Run `dev dashboard`. Your browser opens devkit's dashboard, a page on your Mac that updates by itself while it is open.
It opens on **Now**, which says in one sentence whether anything needs you and when your run lands. Open
**Background** (or press `g` then `b`): your run is there with every step, who owns it, and anything waiting for
you. **Team**, then **Crew** (`g c`), lists the crew members working now. The [Dashboard](../dashboard.md) guide
describes every view.

## Step 5: finish

The run ends when every approver has reviewed the work they own: here, the Code reviewer (Seraph) reviews the Frontend
engineer's page. Ask Codex how to open what it built: a one-page app is a file that opens in your browser. To end the
session, type `/quit`. devkit records the session in the project's handoff note, so the next session starts from where
this one stopped.

## When something goes wrong

| What you see | What to do |
| --- | --- |
| The reply has no `[Firefly> DevKit]` line | Quit Codex, start it again, and ask again. Still missing: run `dev doctor` and follow the fix it prints. A long `AGENTS.md` in the repository can crowd out devkit's rules; keep it short |
| `dev doctor` shows `devkit hooks trusted` failing | Codex skips hooks until they are trusted. Run `dev install --only codex`, or review them in Codex with `/hooks`, then restart Codex |
| A commit or pull request says it was made with Codex | That is an account setting at OpenAI, not a file on your Mac. `dev doctor` fails while it is on; turn it off in your Codex settings. devkit's commands remove the lines either way |
| A message says the guard denied something | Expected: the guard blocked an unsafe or wasteful action and told Codex the right way |
| The work seems stuck | Run `dev crew graph --latest`: a step that says `waiting for decision D1` waits for your answer |
| Codex says you reached a usage limit | `dev agents` shows how much of your plan's usage is left and when it resets |

[Troubleshooting](../troubleshooting.md) has more problems and their fixes.

See also [Where to use devkit](../surfaces.md), [Codex in the ChatGPT app](chatgpt-codex.md), and
[Models and tokens › Codex profiles](../models-and-tokens.md#codex-profiles).
