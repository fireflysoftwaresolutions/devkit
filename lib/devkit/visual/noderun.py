# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""One run of a node driver that starts a browser, so that no browser outlives it.

Playwright starts Chromium detached, in a process group of its own, with its profile under TMPDIR, so stopping node
alone leaves the browser running, with its DevTools port and its profile. `run` gives the driver a session of its own
and TMPDIR in a private folder: a new 0700 folder, deleted when the run ends, or the caller's own. When the driver
does not end within its timeout, fails, or is interrupted, `stop` stops the driver's process group and every process
group whose command line names that folder: the browser's, which names its profile there.

The marks (judge.py), the comparison (compare.py), and the crawl and the walk (map.py) run their drivers through it;
store.py's capture runs capture.cjs the same way.

Interface:

  run(args, stdin="", *, timeout, cwd=None, env=None, tmpdir=None, prefix="devkit-node-", within=None)
      -> subprocess.CompletedProcess (text); raises subprocess.TimeoutExpired once the driver is stopped, OSError
      when it does not start, and ValueError, before it starts, when `tmpdir` is not an absolute folder below the
      root. `cwd` defaults to the folder; `env` to this process's environment, with TMPDIR set.
  stop(proc, folder)     SIGKILL to the driver's process group and to each process group that names `folder` as a
                         path; ValueError, before it stops anything, when `folder` is not absolute or is the root
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile

STOP_WAIT = 10  # seconds for a stopped driver to end and close its output


def _as_path(folder) -> re.Pattern:
    """The run's folder as a path on a command line: where an argument or an option's value (after `=`) starts, and
    followed by `/` or the argument's end. Raises ValueError for a folder that is not absolute, or is the root: a
    relative or empty one would name the paths of any process on this Mac."""
    path = os.path.normpath(folder) if isinstance(folder, str) and os.path.isabs(folder) else None
    if path is None or os.path.dirname(path) == path:
        raise ValueError(f"A node run's folder must be an absolute folder below the root, not {folder!r}.")
    # Playwright joins the profile's path to TMPDIR, which normalizes it as normpath does
    return re.compile(r"(?:^|[\s=])" + re.escape(path) + r"(?=/|\s|$)")


def stop(proc, folder: str) -> None:
    """Stop the driver's process group, and every process group whose command line names the run's folder as a path:
    the browser, which Playwright starts detached (a group of its own), with its profile in TMPDIR. A browser that
    outlived its driver would keep its DevTools port open. A folder that is not absolute is refused (ValueError)
    before anything is stopped."""
    named = _as_path(folder)
    groups = {proc.pid}
    try:
        out = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,pgid=,command="], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and named.search(parts[2]) and parts[1].isdigit():
            groups.add(int(parts[1]))
    for group in groups - {0, 1, os.getpgrp()}:
        try:
            os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def run(args, stdin: str = "", *, timeout, cwd=None, env=None, tmpdir=None, prefix: str = "devkit-node-",
        within=None) -> subprocess.CompletedProcess:
    """`args` run with `stdin` as input, in a session of their own, with TMPDIR set to `tmpdir` (kept) or to a new
    0700 folder under `within` (deleted when the run ends). A run that does not end in `timeout` seconds, that
    fails, or that is interrupted stops with every process it started, the browser included. A `tmpdir` that is not
    absolute is refused (ValueError) before the driver starts, since stop() finds the browser by it."""
    own = tmpdir is None
    if not own:
        _as_path(str(tmpdir))
    # mkdtemp makes the folder 0700, readable by this user alone; under a relative `within` it names it relatively
    folder = os.path.abspath(tempfile.mkdtemp(prefix=prefix, dir=None if within is None else str(within))) if own \
        else str(tmpdir)
    try:
        environ = {**(os.environ if env is None else env), "TMPDIR": folder}
        proc = subprocess.Popen(list(args), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=environ, cwd=str(cwd or folder), start_new_session=True)
        try:
            stdout, stderr = proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            stop(proc, folder)
            try:
                proc.communicate(timeout=STOP_WAIT)
            except subprocess.TimeoutExpired:
                pass
            raise
        except BaseException:
            stop(proc, folder)
            raise
        if proc.returncode != 0:
            stop(proc, folder)
        return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
    finally:
        if own:
            shutil.rmtree(folder, ignore_errors=True)
