---
description: Find unfinished work - stubs, placeholders and TODOs without a task
---

Run `dev stubs` (the uncommitted changes; `--staged` for the next commit, `--all` for the whole repository with $ARGUMENTS) and report each finding with file:line. For each one: build it for real now, or, if deferring it is part of the plan and the developer wants that, record a task with `dev task add "…" --why "…"` and name it on the line (`TODO(T-…)`). Never weaken a finding with a fake reason.
