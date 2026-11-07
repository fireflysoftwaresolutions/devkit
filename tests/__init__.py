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

_hermetic.activate()
