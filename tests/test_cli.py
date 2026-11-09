# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# What `dev` itself tells the reader to type: a command it teaches must be one devkit accepts as taught.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_cli
import contextlib
import io
import shlex
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import cli, lessonlint  # noqa: E402
from test_lessons_base import LessonCase  # noqa: E402

# The one form of `dev lesson add` that every place teaches (the agent profiles, the hooks' hint, AGENTS.md)
LESSON_ADD = ('dev lesson add "…" --happened "…" --cause "…" --prevent "…" --file <glob> '
              '--command "<command words>" --error "<output text>"')


class LessonHintTests(LessonCase):
    def test_an_empty_lesson_list_teaches_the_add_command_its_shape_check_accepts(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["lesson", "ls"])
        self.assertEqual(rc, 0)
        line = next((ln for ln in out.getvalue().splitlines() if "dev lesson add" in ln), "")
        taught = line[line.find("dev lesson add"):]
        self.assertEqual(taught, LESSON_ADD, out.getvalue())
        a = cli.build_parser().parse_args(shlex.split(taught)[1:])
        self.assertEqual(lessonlint.check_shape(" ".join(a.title), a.happened, a.evidence or []), [])


if __name__ == "__main__":
    unittest.main()
