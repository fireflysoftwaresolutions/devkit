---
description: Run only the tests a change can break - failed first, then the blast radius
---

Run `dev test plan` and say what would run and why (changed file → tests, estimate). Then run `dev test`: the tests that failed last time first, then the tests affected by the changes. Report the failures with file:line and the first assertion line. Never run the whole suite to check one fix; if the blast radius is everything, say which file causes it and suggest `dev ci --run` before the push. For new tests, run `dev test prove` and `dev test audit --changed`. Follow the `testing` skill. $ARGUMENTS
