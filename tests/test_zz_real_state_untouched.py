# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The suite-level check: the run left the developer's real devkit as it found it. unittest runs the modules in name
# order, so this one runs last. It fails, naming each file, when a test of this run
#   - opened, removed, renamed or listed a file below the real state, configuration, install or handoff roots, or
#     started a process with the real home (the audit hook of tests/_hermetic.py refuses and records each one), or
#   - left a file below the real state, configuration or handoff roots created, changed or removed: the roots are
#     compared with the snapshot taken before the first test ran. The developer's running sessions keep writing there
#     (devkit's hooks, a run of another checkout), so in an isolated run a change counts only when its path or its
#     new bytes carry this run's temporary home, the only way a process of this run can reach them; the others are
#     listed on stderr. A run that is not isolated has no such mark: every change counts, but the live hooks' own
#     files (_hermetic.LIVE_WRITERS).
# It only reads. Tests carry comments, not docstrings: `dev test prove` leaves out tests with a docstring.
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hermetic  # noqa: E402

_hermetic.activate()
_hermetic.watch_late()


class RealStateUntouched(unittest.TestCase):
    # nothing this run did reached the developer's real devkit state, configuration or handoff repository
    def test_the_run_left_the_real_devkit_as_it_found_it(self):
        found, others = _hermetic.report()
        if others:
            print("\nchanged during the run by other processes (not this run's):\n  " + "\n  ".join(others),
                  file=sys.stderr)
        self.assertEqual([], found, "the test run reached the developer's real devkit:\n  " + "\n  ".join(found))


if __name__ == "__main__":
    unittest.main()
