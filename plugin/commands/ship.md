---
description: "Ship with one CI run: integrate locally, run CI locally, push once"
---

Follow the devkit `delivery` skill for: $ARGUMENTS

1. Run `dev ci` to see what CI runs. When more than one branch or PR must land, merge them first on one local `release/<version>` or `integration/<topic>` branch, and resolve the conflicts there.
2. Run the full local equivalent of CI with `dev ci --run`, which records the evidence that `dev push`, `dev pr ready` and `dev release` check. Fix every failure locally before anything leaves this Mac.
3. Push once with `dev push`. If the branch has no PR yet, open it with `dev pr create --draft`: devkit's template, pre-filled from the commits and the local CI result; write its "What and why" and "Risk and rollback" (the `communicating` skill has the template).
4. Run `dev pr ready` once, when the local suite is green: it starts the one CI run.
5. Arm a watch that wakes you, in the same turn, and name its task id in your reply: `dev ci watch --wait --pr <n>` as a Monitor or background command in Claude Code, in the foreground with a timeout in Codex. Re-arm it when it ends before the run (exit 3; `dev ci watches` prints the command). `dev ci status` is for a look; it wakes nobody.
6. If CI fails, read the error with `dev run view <id> --failed`, reproduce it locally, fix it, run the local suite again, and push once. Never push to "see what CI says."
7. When CI is green and the developer asked for the merge or approves it now: `dev pr merge`, then `dev task reconcile`, then `dev release` by the project's delivery policy (`dev delivery`). Arm a watch on the merge commit (`dev ci watch --wait --pr <n>`) and on the tag (`dev ci watch --wait --tag v<version>`), then download or install the published artifact and check it.
