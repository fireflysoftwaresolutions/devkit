# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""What a `dev` command refuses on purpose, as opposed to devkit's own bug.

A domain error declares itself a refusal by inheriting `Refusal` next to its current base
(`class IdentityError(core.Refusal, ValueError)`), so every `except ValueError` keeps working. `cli.main` catches
`Refusal` once, before `Exception`: it prints `dev <command>: <message>` and the fix on stderr and exits with the
refusal's code, with no crash report. Anything else a command raises is a crash (exit 70) that reaches devkit's bug
registry, so never raise a refusal for a defect.

This module imports nothing, so the pure configuration schema can use it too; `core` re-exports both classes, and
`core.Refusal` is the name to use everywhere else.
"""


class Refusal(Exception):
    """devkit refuses: the message (`str(e)`) says why, `fix` what to run or do instead, and `code` is the exit
    status (2).

    `fix` and `code` default to the class's own, so a subclass can set either as a class attribute. A subclass whose
    `__init__` sets `code` to something that is no exit status (a machine-readable word) exits with its class's
    default."""

    code = 2
    fix = ""

    def __init__(self, message="", fix="", code=None):
        super().__init__(message)
        if fix:
            self.fix = fix
        if code is not None:
            self.code = code


class Busy(Refusal):
    """Another process holds what the command needs; running the command again can work. Exit 75 (EX_TEMPFAIL)."""

    code = 75
