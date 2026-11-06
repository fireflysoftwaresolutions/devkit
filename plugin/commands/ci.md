---
description: Show what CI runs, run it locally, or diagnose the pipelines
---

Run `dev ci` in this repository, then run the listed commands locally in order with `dev ci --run` (matching tool versions where possible) and report which pass or fail. Do not push. If the arguments ask for a review or a diagnosis of the pipelines, run `dev ci audit` instead and report the findings by severity with their fixes (follow the `cicd` skill); `dev ci status` shows live runs on any host. $ARGUMENTS
