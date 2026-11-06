# Ask about your work in Claude Desktop chat and Cowork

This page shows how to use Claude Desktop's **Chat** and **Cowork** tabs with devkit: to ask where things stand, plan
work, and write reports from devkit's records. It also explains why code changes belong in the **Code** tab.

## What chat and Cowork are for

Chat and Cowork do not run Claude Code, so devkit's hooks, guard, and crew are not active there. devkit reaches them
through its own **devkit extension**, which gives Claude tools that read your workspace and handoff repository on this
Mac:

| Ask about | Tool |
| --- | --- |
| Every project and initiative, one project's state, its tasks, a standup, a status report | `devkit_overview`, `devkit_project_status`, `devkit_tasks`, `devkit_standup`, `devkit_report` |
| CI runs, the delivery policy, unfinished work, which tests a change can break, a pipeline audit | `devkit_ci_status`, `devkit_delivery_policy`, `devkit_stubs`, `devkit_test_plan`, `devkit_ci_audit` |
| The crew: who does and approves each kind of work, who works now, and the decisions waiting for you | `devkit_crew` |
| devkit's rules for agents, whole or one section (for example the status block, or how Morpheus routes work) | `devkit_rules` |
| Who works on what, the setup's health, the audit trail, the scheduled jobs, lessons learned | `devkit_team`, `devkit_doctor`, `devkit_audit`, `devkit_jobs`, `devkit_lessons` |
| Add a task, or start, finish, block, unblock, or release one | `devkit_task_add`, `devkit_task_update` |

The two task tools run `dev task`, so a change made here gets the same checks and the same audit trail as one made in
Terminal. Every other tool only reads.

## Install the extension

The setup wizard installs it when Claude Desktop is on your Mac. To install it later, or to check it:

```bash
dev install --only desktop   # builds the extension and opens it in Claude Desktop: choose Install
dev desktop status           # installed version, turned on or off, and where it finds devkit
```

In Claude Desktop, **Settings › Extensions** lists **devkit** with its icon. It needs no folder setting: it finds
devkit where the installer put it.

## Try it

Open the **Chat** tab, start a new chat, and ask:

```text
Where do my projects stand, and is anything waiting for me?
```

Claude calls `devkit_overview` (Claude Desktop may ask you to allow the tool the first time) and answers with your
projects, their status, and their Now tasks. A reply about a project's development starts with the status block, the bold
`[Firefly> DevKit]` line followed by the **Done**, **Check**, and **Next** bullets.

More questions to try:

```text
Who on the crew is working right now, and which decisions wait for me?
Write this week's status report for the reading-list project.
What do devkit's rules say about the status block?
Add a task to reading-list: "Sort the list by title", for next week.
```

In **Cowork**, the same tools are there for longer work, such as a plan or a report that Claude writes for you.

## Why code changes go to the Code tab

When you ask for a code change in chat or Cowork, Claude tells you to open the project in the **Code** tab. That is on
purpose:

- **The guard runs only in the Code tab.** It is what stops an agent from deleting work, pushing to `main`, or filling
  your disk.
- **The crew runs only in the Code tab.** Morpheus can route the work to its crew member and have it approved there.
- **The status block is checked only in the Code tab.** Chat is asked for it, but nothing checks it or records it.
- **The handoff note is kept current from the Code tab.** Sessions there are recorded, so the next session starts from
  where the last one stopped.

Follow [Your first task in Claude Desktop's Code tab](claude-desktop.md) to make the change.

## When something goes wrong

| What you see | What to do |
| --- | --- |
| Claude has no devkit tools | Run `dev install --only desktop` and choose **Install**. Check that **devkit** is turned on in **Settings › Extensions** |
| The only devkit tool is `devkit_status`, and it says devkit is not found | Install devkit ([Getting started](../getting-started.md)). If devkit lives in another folder, set it in the extension's settings |
| A new devkit tool is missing after `dev update` | Quit Claude Desktop (Command-Q) and open it again; the extension starts the updated devkit |
| `dev desktop status` shows an older version | Run `dev install --only desktop` and choose **Install** to replace it |

See also [Where to use devkit](../surfaces.md) and [Agents › The devkit extension](../agents.md#the-devkit-extension).
