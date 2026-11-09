# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Python sources carry no invisible or look-alike characters.

A file-writing tool can turn a ``\\uXXXX`` escape into the character itself. Bidirectional controls and zero-width
characters then hide in the source ("Trojan Source"): the code reads one way and runs another. Write them as escapes.

Refused everywhere, strings and comments included:
- bidirectional controls U+202A to U+202E and U+2066 to U+2069;
- zero-width characters U+200B to U+200D, U+2060 and U+FEFF (a leading byte-order mark too).

Refused in code, and in strings and comments unless the file is on LOOKALIKE_ALLOWLIST:
- the hyphen U+2010, the non-breaking hyphen U+2011 and the no-break space U+00A0, which look like ``-`` and `` ``.

This file writes every such character with ``chr()`` so it passes its own scan.
"""
import io
import os
import random
import re
import subprocess
import tempfile
import tokenize
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCANNED_DIRS = ("lib", "bin", "tools", "tests")

BIDI = tuple(range(0x202A, 0x202F)) + tuple(range(0x2066, 0x206A))
ZERO_WIDTH = (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)
HIDDEN = BIDI + ZERO_WIDTH
LOOKALIKE = (0x2010, 0x2011, 0x00A0)

# Files allowed to keep a look-alike dash or space inside a string or comment (never in code, and never a hidden
# character), as {"path/relative/to/the/repository.py": "why the literal character is needed"}.
LOOKALIKE_ALLOWLIST = {}

_FORBIDDEN = re.compile("[" + "".join(re.escape(chr(c)) for c in HIDDEN + LOOKALIKE) + "]")
_LOOKALIKE = frozenset(chr(c) for c in LOOKALIKE)
_LITERAL_TOKENS = frozenset({"STRING", "COMMENT", "FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END",
                             "TSTRING_START", "TSTRING_MIDDLE", "TSTRING_END"})


def _literal_spans(text):
    """{line: [(start col, end col)]} of the strings and comments, or None when the source does not tokenize."""
    spans = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tokenize.tok_name[tok.type] not in _LITERAL_TOKENS:
                continue
            (srow, scol), (erow, ecol) = tok.start, tok.end
            for row in range(srow, erow + 1):
                spans.setdefault(row, []).append((scol if row == srow else 0,
                                                  ecol if row == erow else float("inf")))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return None
    return spans


def scan_text(text, lookalikes_allowed_in_literals=False):
    """Each forbidden character as (line, column, "U+XXXX", reason); lines count from 1, columns from 0."""
    found = []
    spans = None
    for lineno, line in enumerate(text.split("\n"), 1):
        for match in _FORBIDDEN.finditer(line):
            ch, col = match.group(), match.start()
            code = "U+%04X" % ord(ch)
            if ch not in _LOOKALIKE:
                reason = "leading byte-order mark" if (lineno, col, ch) == (1, 0, chr(0xFEFF)) else "hidden character"
                found.append((lineno, col, code, reason))
                continue
            if spans is None:
                # Tokenize without a leading BOM so the columns stay right; the BOM is reported above.
                spans = _literal_spans(text[1:] if text.startswith(chr(0xFEFF)) else text) or {}
                if text.startswith(chr(0xFEFF)) and 1 in spans:
                    spans[1] = [(s + 1, e + 1) for s, e in spans[1]]
            in_literal = any(s <= col < e for s, e in spans.get(lineno, ()))
            if not in_literal:
                found.append((lineno, col, code, "look-alike character in code"))
            elif not lookalikes_allowed_in_literals:
                found.append((lineno, col, code, "look-alike character in a string or comment"))
    return found


def scan_file(path, rel):
    """The findings of one file as "rel:line:col: U+XXXX reason" lines."""
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        return ["%s: not UTF-8 (%s)" % (rel, exc)]
    return ["%s:%d:%d: %s %s" % (rel, line, col + 1, code, reason)
            for line, col, code, reason in scan_text(text, rel in LOOKALIKE_ALLOWLIST)]


def python_sources(root):
    """The .py files under the scanned folders, relative to root: git's tracked and new files, or a directory walk
    when root is no git checkout (an exported copy)."""
    if (root / ".git").exists():
        try:
            out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                                  "--exclude-standard", "--", *SCANNED_DIRS],
                                 capture_output=True, check=True, timeout=30).stdout
            names = {n for n in out.decode("utf-8").split("\0") if n.endswith(".py")}
            return sorted(n for n in names if (root / n).is_file())
        except (OSError, subprocess.SubprocessError):
            pass
    found = []
    for top in SCANNED_DIRS:
        for dirpath, dirnames, filenames in os.walk(root / top):
            dirnames[:] = [d for d in dirnames if d != "__pycache__" and not d.startswith(".")]
            found.extend(Path(dirpath, f).relative_to(root).as_posix() for f in filenames if f.endswith(".py"))
    return sorted(found)


class RepositorySourceTests(unittest.TestCase):
    def test_the_python_sources_carry_no_hidden_or_lookalike_characters(self):
        problems = [p for rel in python_sources(REPO) for p in scan_file(REPO / rel, rel)]
        self.assertEqual(problems, [], "write these characters as \\uXXXX escapes:\n" + "\n".join(problems))

    def test_every_allowlisted_file_exists_and_says_why(self):
        for rel, why in LOOKALIKE_ALLOWLIST.items():
            self.assertTrue((REPO / rel).is_file(), rel)
            self.assertTrue(str(why).strip(), rel)


# The code points the scanner must refuse, listed by hand so a shortened constant above cannot shorten the tests.
MUST_REFUSE_HIDDEN = (0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069,
                      0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)
MUST_REFUSE_LOOKALIKE = (0x2010, 0x2011, 0x00A0)


class ScannerTests(unittest.TestCase):
    """The scanner reports each forbidden character it must, with its file, line and code point."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _scan(self, text, rel="lib/sample.py"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return scan_file(path, rel)

    def test_each_forbidden_character_in_code_is_reported_with_file_line_and_code_point(self):
        for cp in MUST_REFUSE_HIDDEN + MUST_REFUSE_LOOKALIKE:
            with self.subTest(code_point="U+%04X" % cp):
                found = self._scan("import os\nvalue = 1" + chr(cp) + "\n")
                self.assertEqual(len(found), 1, found)
                self.assertTrue(found[0].startswith("lib/sample.py:2:10: U+%04X " % cp), found)

    def test_hidden_characters_are_refused_in_strings_and_comments_even_when_allowlisted(self):
        LOOKALIKE_ALLOWLIST["lib/sample.py"] = "a test fixture"
        try:
            for cp in MUST_REFUSE_HIDDEN:
                with self.subTest(code_point="U+%04X" % cp):
                    found = self._scan('x = "a%sb"  # c%sd\n' % (chr(cp), chr(cp)))
                    self.assertEqual([f.split(" ", 2)[:2] for f in found],
                                     [["lib/sample.py:1:7:", "U+%04X" % cp], ["lib/sample.py:1:15:", "U+%04X" % cp]])
        finally:
            del LOOKALIKE_ALLOWLIST["lib/sample.py"]

    def test_lookalikes_in_strings_and_comments_are_refused_unless_the_file_is_allowlisted(self):
        for cp in MUST_REFUSE_LOOKALIKE:
            text = 'x = "a%sb"\n# c%sd\ny = """\nline%s\n"""\n' % (chr(cp), chr(cp), chr(cp))
            with self.subTest(code_point="U+%04X" % cp):
                found = self._scan(text)
                self.assertEqual([f.split(" ")[0] for f in found],
                                 ["lib/sample.py:1:7:", "lib/sample.py:2:4:", "lib/sample.py:4:5:"])
                self.assertTrue(all("in a string or comment" in f for f in found), found)
                LOOKALIKE_ALLOWLIST["lib/sample.py"] = "a test fixture"
                try:
                    self.assertEqual(self._scan(text), [])
                    in_code = self._scan(text + "z = 1" + chr(cp) + "\n")
                    self.assertEqual(len(in_code), 1, in_code)
                    self.assertIn("lib/sample.py:6:6: U+%04X look-alike character in code" % cp, in_code[0])
                finally:
                    del LOOKALIKE_ALLOWLIST["lib/sample.py"]

    def test_a_leading_byte_order_mark_is_refused(self):
        found = self._scan(chr(0xFEFF) + "import os\n")
        self.assertEqual(found, ["lib/sample.py:1:1: U+FEFF leading byte-order mark"])

    def test_a_lookalike_after_a_leading_byte_order_mark_keeps_its_column(self):
        found = self._scan(chr(0xFEFF) + 'x = "a' + chr(0x2011) + 'b"\n')
        self.assertEqual(found, ["lib/sample.py:1:1: U+FEFF leading byte-order mark",
                                 "lib/sample.py:1:8: U+2011 look-alike character in a string or comment"])

    def test_a_lookalike_in_code_after_a_leading_byte_order_mark_is_not_taken_for_the_next_string(self):
        # Python 3.9 reads the no-break space as an error token right before the string instead of raising.
        LOOKALIKE_ALLOWLIST["lib/sample.py"] = "a test fixture"
        try:
            found = self._scan(chr(0xFEFF) + "x =" + chr(0x00A0) + '"ab"\n')
        finally:
            del LOOKALIKE_ALLOWLIST["lib/sample.py"]
        self.assertEqual(found, ["lib/sample.py:1:1: U+FEFF leading byte-order mark",
                                 "lib/sample.py:1:5: U+00A0 look-alike character in code"])

    def test_escaped_characters_and_plain_unicode_are_accepted(self):
        text = 'BOM = "\\ufeff"\nRLO = "\\u202e"\nNBH = "\\u2011"\nNAME = "caf\u00e9 \u2014 \u2192"  # \u00e9\n'
        self.assertEqual(self._scan(text), [])

    def test_a_file_that_is_not_utf8_is_reported(self):
        path = self.root / "lib" / "latin.py"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x = '\xe9'\n")
        found = scan_file(path, "lib/latin.py")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("lib/latin.py: not UTF-8"), found)

    def test_a_hidden_character_anywhere_is_reported_at_its_exact_place(self):
        # Seeded, so a failure repeats: any hidden character at any place in a real module is found exactly there.
        rng = random.Random(20261108)
        source = (REPO / "tests" / "test_source_characters.py").read_text(encoding="utf-8")
        lines = source.split("\n")
        for _ in range(200):
            cp = rng.choice(MUST_REFUSE_HIDDEN)
            row = rng.randrange(len(lines))
            col = rng.randrange(len(lines[row]) + 1)
            mutated = list(lines)
            mutated[row] = lines[row][:col] + chr(cp) + lines[row][col:]
            reason = "leading byte-order mark" if (row, col, cp) == (0, 0, 0xFEFF) else "hidden character"
            self.assertEqual(scan_text("\n".join(mutated)), [(row + 1, col, "U+%04X" % cp, reason)])

    def test_without_git_the_sources_are_found_by_walking_the_scanned_folders(self):
        for rel in ("lib/devkit/a.py", "bin/b.py", "tools/c.py", "tests/d.py", "lib/notes.txt",
                    "lib/__pycache__/e.py", "docs/f.py", "g.py"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text("x = 1\n", encoding="utf-8")
        self.assertEqual(python_sources(self.root), ["bin/b.py", "lib/devkit/a.py", "tests/d.py", "tools/c.py"])

    def test_with_git_the_tracked_and_new_sources_are_found_and_ignored_ones_are_not(self):
        for rel in ("lib/a.py", "tests/b.py", "lib/ignored.py", "docs/c.py"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text("x = 1\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("lib/ignored.py\n", encoding="utf-8")
        git = ["git", "-C", str(self.root), "-c", "user.email=t@example.com", "-c", "user.name=t"]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["add", "lib/a.py"], check=True)
        subprocess.run(git + ["commit", "-qm", "init"], check=True)
        self.assertEqual(python_sources(self.root), ["lib/a.py", "tests/b.py"])


if __name__ == "__main__":
    unittest.main()
