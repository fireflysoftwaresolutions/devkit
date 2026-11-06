---
description: "Ship with one CI run: integrate locally, run CI locally, push once"
---

Follow the devkit `delivery` skill for: $ARGUMENTS. Run `dev ci` to see what CI runs; integrate the relevant branches on one local `release/…` or `integration/…` branch if more than one is involved; run the full local equivalent of CI; only when it is green, push once and open one PR (`gh pr create --fill`), then `gh pr checks --watch`. If CI fails, reproduce locally before any new push.
