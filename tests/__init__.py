# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The suite runs as a package (python3 -m unittest discover -s tests -t .), so this runs before any test module
# imports devkit: the whole run gets a temporary home and the developer's real devkit stays out of reach
# (tests/_hermetic.py). _hermetic is loaded by its top-level name, the one a test module run alone uses, so both
# share one module and one temporary home.
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import _hermetic  # noqa: E402

_PART = os.environ.get("DEVKIT_TEST_PART", "")  # read first: activation drops every DEVKIT_* variable of the run
_hermetic.activate()

# CI runs the suite in parts, one per matrix leg, with the local gate's own command (.github/workflows/ci.yml): with
# DEVKIT_TEST_PART set, part k of PARTS loads every PARTS-th test module by name, then the real-state check of its
# own run, so the parts together load each module once. Without a part (dev ci --run, a developer's run) load_tests
# is not defined, and discovery loads every module as it always has. The run's own processes never see the part
# (activation dropped it), so a discovery a test starts loads every module.
_LAST = "test_zz_real_state_untouched"


def part_modules(names, part: int, parts: int) -> list:
    """The test modules part `part` (from 1) of `parts` loads, from every module name in `names`: every parts-th one
    by name, then the real-state check."""
    if not 1 <= part <= parts:
        raise ValueError(f"no part {part} of {parts}: DEVKIT_TEST_PART counts from 1 to PARTS")
    mine = [n for k, n in enumerate(sorted(n for n in names if n != _LAST)) if k % parts == part - 1]
    if not mine:
        raise ValueError(f"no test module in part {part} of {parts}")
    return mine + ([_LAST] if _LAST in names else [])


if _PART:
    import fnmatch
    import traceback
    import unittest

    def _stand_in(name: str, error: BaseException):
        """One test named after a module that cannot be loaded: it is skipped when the module asked for that, and
        fails with the import error otherwise, as discovery's own stand-ins do, so the rest of the part still runs."""
        def stand_in():
            raise error
        stand_in.__name__ = name
        return unittest.FunctionTestCase(stand_in)

    def load_tests(loader, standard_tests, pattern):
        part, parts = int(_PART), int(os.environ.get("PARTS") or 1)
        names = [f[:-3] for f in os.listdir(_HERE)  # the files discovery imports: test*.py with a module's name
                 if fnmatch.fnmatch(f, pattern or "test*.py") and unittest.loader.VALID_MODULE_NAME.match(f)]
        for name in part_modules(names, part, parts):
            module = f"{__name__}.{name}"
            try:
                standard_tests.addTests(loader.loadTestsFromName(module))
            except unittest.SkipTest as skip:
                standard_tests.addTest(_stand_in(module, skip))
            except Exception:  # noqa: BLE001 - any error a module raises when it is imported
                standard_tests.addTest(_stand_in(module, ImportError(
                    f"Failed to import test module: {module}\n{traceback.format_exc()}")))
        return standard_tests
