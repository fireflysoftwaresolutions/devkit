# Tutorial: your first task with Codex in the ChatGPT app

In this tutorial you open a project with Codex in the ChatGPT desktop app, check that devkit is active, and give
devkit's crew a real task: a small reading-list web page. On the way you learn to read what devkit shows you: the
status block, the progress updates, the moments it waits for you, the crew graph, and the dashboard. It takes about
30 minutes, most of it while the crew works.

The ChatGPT app for Mac includes Codex, OpenAI's coding agent. When you work in a folder on your Mac, it runs its own
copy of Codex with the same `~/.codex` configuration as Codex in Terminal: devkit's rules, hooks, guard, skills, and
crew roles.

> **Not verified live yet.** devkit's live test of Codex waits for the Codex usage quota of the test account to reset
> on 2026-11-09. This page follows devkit's code, its tests, the Codex configuration that `dev doctor` checks, and
> OpenAI's documentation of the app, not a recorded session in the app. The names of buttons can differ in your
> version of the app. If devkit behaves differently there, `dev doctor` and `dev canary` show it; see
> [When something goes wrong](#when-something-goes-wrong).

## What you need

- devkit installed on your Mac, with Codex chosen in the setup wizard and every line of `dev doctor` showing `✓`. If
  you have not installed it yet, follow
  [Your first install, step by step](../getting-started.md#your-first-install-step-by-step).
- The ChatGPT app for Mac, signed in with a ChatGPT account that includes Codex.
- If the app was open while you installed or updated devkit, quit it (Command-Q) and open it again, so it reads the
  new configuration.

## Step 1: create the project folder

A project is a folder with your code in it. Create one in Terminal (Command-Space, type `Terminal`, press Return):

```bash
dev new reading-list
```

It creates `~/Development/_local/reading-list` and prints that path. For a project from GitHub, run
`dev clone <owner>/<repo>` instead; it goes to `~/Development/<owner>/<repo>`.

## Step 2: open the project in Codex

1. In the ChatGPT app, choose **Codex** in the box where you type.
2. Under **Choose where to work**, open a folder and pick `~/Development/_local/reading-list`. In the folder window,
   Command-Shift-G lets you type the path.
3. Start a **New chat** in that folder.

Work in the folder on your Mac. A Codex cloud environment runs on OpenAI's computers, without devkit.

## Step 3: check that devkit is active

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

That block is the **status block**. Every development reply starts with it, so it also proves that Codex loaded
devkit's rules. If the reply has no such line, go to [When something goes wrong](#when-something-goes-wrong).

## Step 4: give the crew a task

Type this request and press Return:

```text
Build a one-page web app in this folder where I can add books to a reading list and mark them as read. Keep the
list in the browser, add tests, and write a short README that explains how to open it.
```

What happens, and what you see:

1. **devkit routes the request.** Along with your request, devkit tells Codex which kind of work this is and who on the
   crew does it: front-end work, done by the Frontend engineer (Trinity) and approved by the Code reviewer (Seraph).
2. **Codex answers as Morpheus, the tech lead.** He posts a plan and an estimate. Work under ten minutes he does
   himself; this request gets steps for the crew.
3. **The crew starts.** Morpheus launches each member as a Codex agent role, for example `trinity`. At most four run at
   once.
4. **Progress updates arrive** after each finished step, and every ten minutes or so while a long step runs.
5. **The final report** closes the run: what changed, the evidence, what is not done, and the next step.

## Step 5: read what devkit shows you

### The status block, line by line

```markdown
**[Firefly> DevKit]** reading-list · `main` · Reading list
- **Done:** the page adds books and marks them as read; the list stays after a reload.
- **Check:** ✓ 6 tests pass in a headless browser
- **Progress:** 3 of 4 steps · 31 min of ~45 · ETA 10–15 min
- **Crew:** Trinity ✓ the page · Mouse ✓ the tests · Seraph ▸ the review
- **You:** nothing yet; I ask before anything leaves this Mac.
- **Next:** Seraph finishes the review, then I write the final report.
```

| Line | What it tells you |
| --- | --- |
| Header | The project, the branch, and the task in a few words |
| **Done** | What was done or found, in one sentence |
| **Check** | The proof. `✓` checked in this session, `⚠` partly or not checked, `✗` failed, `–` nothing to check |
| **Progress** | Only while a run is going: steps done, time spent, and when it is likely to finish |
| **Crew** | Only when crew members work: each one with `▸` working, `✓` done, or `✗` failed |
| **You** | Only when something waits for you: a decision, an approval, or an action on your Mac or account |
| **Next** | The next step, and who takes it |

macOS shows a notification when a run finishes or gets blocked.

### When devkit waits for you

- **A decision that is yours.** A crew member reaches a choice, for example whether finished books move to the bottom
  of the list. The member stops, and Codex asks you and gives the options and a recommendation. Answer in the
  chat; Morpheus records it and the member goes on.
- **An action that cannot be undone or leaves your Mac**, such as a push to GitHub, a pull request, a merge, a release,
  or deleting data. It waits for your OK, unless you approved it in advance.

devkit's guard blocks unsafe or wasteful commands and tells Codex the right way. Where Claude Code would ask you, for
example before a write to your Desktop, the guard refuses, because Codex has no "ask" answer.

### The crew graph

In Terminal, run:

```bash
dev crew graph --latest
```

It draws the newest run on this Mac, with Morpheus, every step with its state and owner, what each step waits for, and the
decisions waiting for you. The [Claude Code tutorial](claude-code.md#the-crew-graph) shows a full example and explains
the symbols.

### The dashboard

Run `dev dashboard` in Terminal. Your browser opens devkit's dashboard, a page on your Mac that updates by itself
while it is open. It opens on **Now**, which says in one sentence whether anything needs you and when your run
lands. Open **Background** (or press `g` then `b`): your run is there with every step, who owns it, and
anything waiting for you. The [Dashboard](../dashboard.md) guide describes every view.

## Step 6: finish

The run ends when every approver has reviewed the work they own: here, Seraph reviews Trinity's page. Ask Codex how to
open what it built: a one-page app is a file that opens in your browser. devkit records the session in the project's
handoff note, so the next session, in the app or in Terminal, starts from where this one stopped.

## When something goes wrong

| What you see | What to do |
| --- | --- |
| The reply has no `[Firefly> DevKit]` line | Check that the chat works in the folder on your Mac, not in a cloud environment. Quit the app, open it again, and start a new chat in the folder. Still missing: run `dev doctor` in Terminal and follow the fix it prints |
| `dev doctor` shows `devkit hooks trusted` failing | Codex skips hooks until they are trusted. Run `dev install --only codex`, or review the hooks in the app's settings, then quit and reopen the app |
| `dev doctor` says the ChatGPT app does not see the devkit plugin | Run `dev install --only codex`, then quit and reopen the app |
| A commit or pull request says it was made with Codex | That is an account setting at OpenAI. `dev doctor` fails while it is on; turn it off in your Codex settings |
| The work seems stuck | Run `dev crew graph --latest` in Terminal: a step that says `waiting for decision D1` waits for your answer |
| Codex says you reached a usage limit | `dev agents` shows how much of your plan's usage is left and when it resets |

[Troubleshooting](../troubleshooting.md) has more problems and their fixes.

See also [Where to use devkit](../surfaces.md), [Codex CLI](codex-cli.md), and
[Agents › Claude Desktop and the ChatGPT app](../agents.md#claude-desktop-and-the-chatgpt-app).
