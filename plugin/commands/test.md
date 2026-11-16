---
description: Run only the tests a change can break - failed first, then the blast radius
---

Run `dev test plan` and say what would run and why (changed file → tests, estimate). Then run `dev test`: the tests that failed last time first, then the tests affected by the changes. Report the failures with file:line and the first assertion line. Never run the whole suite to check one fix; if the blast radius is everything, say which file causes it and suggest `dev ci --run` before the push. For new tests, run `dev test prove` and `dev test audit --changed`. For lesson-prevention receipts, use `dev test prove --isolated-imports` only for eligible direct unittest checks and report its eligibility; ordinary prove supplies causal evidence only. Follow the `testing` skill. $ARGUMENTS

Treat an interrupted unittest invocation as incomplete even if it prints a nested summary or exits 0. Its outer
completion receipt determines whether the selection finished. Preserve unresolved failures; an incomplete run
cannot establish that their tests were deleted. After an interrupted CI test step, `dev ci --run --resume` reruns
the whole affected step, including when the interrupted run was partial or the record lacks completion evidence.
