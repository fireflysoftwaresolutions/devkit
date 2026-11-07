# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# A module-level name defined twice in one module silently replaces the first definition for every function that reads
# it at call time. Two branches merged into 26.11.4 each added a `_NAME` to lib/devkit/guard.py; the second one turned
# the vendor CLI allowlist's name slot into a regular expression, and `claude mcp get <name>` was refused. Each
# module's top-level names (assignments, functions, classes) are defined once.
# Run: python3 -m unittest tests.test_module_names
import ast
import collections
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib" / "devkit"


def defined_twice(source: str) -> list:
    """The names a module's top level defines more than once, by assignment, function or class."""
    seen = collections.Counter()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            seen.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            seen[node.target.id] += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            seen[node.name] += 1
    return sorted(name for name, n in seen.items() if n > 1)


class ModuleNames(unittest.TestCase):
    def test_each_top_level_name_is_defined_once(self):
        files = sorted(LIB.rglob("*.py"))
        self.assertGreater(len(files), 10)
        twice = {str(f.relative_to(LIB)): defined_twice(f.read_text(encoding="utf-8")) for f in files}
        self.assertEqual({f: names for f, names in twice.items() if names}, {})

    def test_the_check_sees_a_name_defined_twice(self):
        self.assertEqual(defined_twice('_NAME = "<name>"\ndef f(): pass\n_NAME = 1\nclass f: pass\n'), ["_NAME", "f"])


if __name__ == "__main__":
    unittest.main()
