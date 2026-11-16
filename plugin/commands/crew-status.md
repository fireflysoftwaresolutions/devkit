---
description: The crew - who is working, what is ready to launch, and who should take a task
---

Run `dev crew` and summarize who is working (lanes, minutes), the open messages and decisions, and the mode.
Then run `dev crew next` and list what is ready to launch, queued, or waiting and why.
If arguments describe a task, run `dev crew route "$ARGUMENTS"` and recommend the member with its reasons and
the `dev crew brief` line. Follow the `crew` skill.

Message previews are indexes, not receipts. Read a referenced message with `dev crew read REF --json`, follow
all cursors, and acknowledge its exact digest and generation only after reading it fully. Acknowledgment is not
an answer or approval.
