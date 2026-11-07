# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The zsh completion (shell/completions/_dev) offers what the command line takes: `dev prompt [refresh]`,
# `dev setup prompt [--remove]`, `dev config set` and `dev progress step --approves`. Each test runs the real _dev
# function in zsh for a typed command line, with zsh's _describe, _arguments and _files replaced by functions that
# print what they were offered.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_cli_completion
import shutil
import subprocess
import unittest
from pathlib import Path

COMPLETION = Path(__file__).resolve().parents[1] / "shell" / "completions" / "_dev"
STUBS = r"""
_describe() { local arr=$2; local v; for v in "${(@P)arr}"; do print -r -- "D ${v%%:*}"; done }
_arguments() { local v; for v in "$@"; do v=${v#\(*\)}; v=${v#\*}; print -r -- "A ${v%%\[*}"; done }
_files() { print -r -- "F"; }
"""


def offered(*typed):
    # what zsh offers after `dev <typed…> <TAB>`: the candidates _describe and _arguments received
    words = " ".join(f"'{w}'" for w in ("dev", *typed, ""))
    script = f"{STUBS}\nsource {COMPLETION} >/dev/null 2>&1\nwords=({words}); CURRENT={len(typed) + 2}; _dev\n"
    r = subprocess.run(["zsh", "-f", "-c", script], capture_output=True, text=True, timeout=30)
    if r.returncode not in (0, 1):
        raise AssertionError(f"zsh exited {r.returncode}: {r.stderr}")
    return {ln.split(" ", 1)[1] for ln in r.stdout.splitlines() if ln[:2] in ("D ", "A ")}


@unittest.skipUnless(shutil.which("zsh"), "zsh is not installed")
class CompletionTests(unittest.TestCase):
    def test_dev_offers_the_prompt_command_and_its_refresh(self):
        self.assertIn("prompt", offered())
        self.assertEqual(offered("prompt"), {"refresh"})

    def test_dev_setup_offers_prompt_and_its_remove_option(self):
        self.assertIn("prompt", offered("setup"))
        self.assertIn("--remove", offered("setup", "prompt"))

    def test_dev_config_offers_set(self):
        self.assertIn("set", offered("config"))

    def test_dev_progress_step_offers_approves(self):
        self.assertIn("--approves", offered("progress", "step", "reviewed the api"))
        self.assertNotIn("--approves", offered("progress", "block", "waiting"))


if __name__ == "__main__":
    unittest.main()
