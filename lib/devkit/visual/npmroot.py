# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""npm's global folder (`npm root -g`), where the pinned @playwright/cli and axe-core live (config/npm-globals.txt),
for devkit's Python: the browser of the comparison and the marks (compare.py), the folder `dev visual` names for
axe-core (commands.py), and the packages step and `dev doctor`'s row of pinned packages (wizard.py).

The rule is npmroot.cjs's, which the capture driver and axe follow: one process asks npm once, and a good answer (exit 0
and one absolute folder) is kept for the process. On a busy Mac npm can fail, time out, or print nothing; such an
answer is asked again once, and when it is still no folder the caller hears what npm did, so it never reads an empty
answer as a relative path and reports a missing install. An answer that is no folder is not kept: the next lookup asks
again.

Interface:

    global_root() -> Path     the absolute folder npm named
    NoFolder                  raised when npm named none on either try: str() is
                              "npm root -g gave no folder (<what npm did on its last try>, twice)"
"""
from __future__ import annotations

import errno
import os
import signal
import subprocess
from pathlib import Path

TRIES = 2
TIMEOUT = 60  # seconds, one try

_kept = None  # the good answer, for the rest of the process


class NoFolder(Exception):
    """npm named no folder on either try; `why` is what it did on the last one ("it printed nothing", …)."""

    def __init__(self, why: str):
        super().__init__(f"npm root -g gave no folder ({why}, twice)")
        self.why = why


def _ask():
    """One try. -> (the folder, None) or (None, what npm did)."""
    try:
        npm = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, errors="replace", timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f"it did not answer in {TIMEOUT} s"
    except OSError as e:
        return None, f"it could not start ({errno.errorcode.get(e.errno, type(e).__name__)})"
    if npm.returncode < 0:
        return None, f"it was stopped by {signal.Signals(-npm.returncode).name}"
    if npm.returncode != 0:
        return None, f"it failed with exit {npm.returncode}"
    out = npm.stdout.strip()
    if not out:
        return None, "it printed nothing"
    if "\n" in out or not os.path.isabs(out):
        return None, "it printed no absolute folder"
    return Path(out), None


def global_root() -> Path:
    """npm's global folder; NoFolder when npm named none on either try."""
    global _kept
    if _kept is None:
        why = None
        for _ in range(TRIES):
            root, why = _ask()
            if root:
                _kept = root
                break
        else:
            raise NoFolder(why)
    return _kept
