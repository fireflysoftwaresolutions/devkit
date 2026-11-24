# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""npm's global folder (`npm root -g`), where the pinned @playwright/cli and axe-core live (config/npm-globals.txt),
for devkit's Python: the browser of the comparison and the marks (compare.py), the folder `dev visual` names for
axe-core (commands.py), and the packages step and `dev doctor`'s row of pinned packages (wizard.py).

The rule is npmroot.cjs's, which the capture driver and axe follow: one process asks npm once, and a good answer (exit 0
and one absolute folder) is kept for the process. On a busy Mac npm can fail, time out, or print nothing; such an
answer is asked again once, and when it is still no folder the caller hears what npm did, so it never reads an empty
answer as a relative path and reports a missing install. Each try waits TIMEOUT seconds, or the limit its caller gives:
the wizard and `dev doctor` give a shorter one, since nothing moves on their screen while npm thinks. An npm missing
from PATH is not asked again: the answer names the command that installs it. An answer that is no folder is not kept:
the next lookup asks again (the wizard keeps it for the rest of its run, wizard.py).

Interface:

    global_root(timeout=None) -> Path   the absolute folder npm named; `timeout` is the seconds a try waits (TIMEOUT)
    NoFolder                            raised when npm named none: str() is "npm root -g gave no folder (<why>)",
                                        where <why> is what npm did on its last try and ", twice", or
                                        "npm is not installed: run dev install --only tools"
"""
from __future__ import annotations

import errno
import os
import signal
import subprocess
from pathlib import Path

TRIES = 2
TIMEOUT = 25  # seconds a try waits unless its caller gives a limit; npmroot.cjs's is the same, so two tries fit the
#               time a capture leaves for them (store.CAPTURE_TIMEOUT less capture.cjs's own bound), with 10 s to spare
NOT_INSTALLED = "npm is not installed: run dev install --only tools"

_kept = None  # the good answer, for the rest of the process


class NoFolder(Exception):
    """npm named no folder; `why` says what it did ("it printed nothing, twice", NOT_INSTALLED, …)."""

    def __init__(self, why: str):
        super().__init__(f"npm root -g gave no folder ({why})")
        self.why = why


def _ask(timeout):
    """One try, waiting `timeout` seconds. -> (the folder, None) or (None, what npm did)."""
    try:
        npm = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"it did not answer in {timeout} s"
    except FileNotFoundError:
        return None, NOT_INSTALLED
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


def global_root(timeout=None) -> Path:
    """npm's global folder, each try waiting `timeout` seconds (TIMEOUT); NoFolder when npm named none."""
    global _kept
    if _kept is None:
        why = None
        for _ in range(TRIES):
            root, why = _ask(timeout or TIMEOUT)
            if root:
                _kept = root
                break
            if why == NOT_INSTALLED:  # a second ask cannot find it either
                raise NoFolder(why)
        else:
            raise NoFolder(f"{why}, twice")
    return _kept
