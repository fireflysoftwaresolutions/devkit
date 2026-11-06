# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
# devkit: allow-stub-words (this file holds the markers it detects)
"""`dev test audit`: find tests that cannot fail for a real bug.

Agents (and people in a hurry) write tests that test nothing: no assertion, `assert True`, `expect(x).toBe(x)`, an
empty body, a try/except that swallows the failure, a check that a mock returns what it was told to return. This
module finds them mechanically, per language:

  Python       ast: pytest functions and unittest methods named test*, also inside classes
  JS / TS      it()/test() blocks (.each, .skip, describe nesting, async, arrow and function bodies), found with a
               scanner that understands strings, template literals, regular expressions, comments and JSX
  Java/Kotlin  JUnit 4/5 @Test, @ParameterizedTest, @RepeatedTest, @TestFactory, @TestTemplate
  Go           func TestX(t *testing.T)
  PHP          PHPUnit test* methods, /** @test */ and #[Test]; Pest it() and test()

Rules. Precision comes first: a false positive on a good test is worse than a miss, so a rule that is unsure stays
quiet (unknown helpers and base classes count as assertions, conditional skips explain themselves, polling loops
are not sleeps).

  high    no-assertion · trivial-assertion · empty-test · swallowed-failure     the test cannot fail for a real bug
  medium  skipped-without-reason · sleep-in-test · duplicate-test · mock-only · snapshot-only · no-production-reference
  low     debug-output · vague-name

API: audit(paths, top) -> findings · changed_test_files(top, since) · stop_gate(top, files) for the Stop hook ·
add_arguments(p) and cmd(a) for `dev test audit`.
"""
from __future__ import annotations

import ast
import bisect
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

RULES = {
    "no-assertion": "high", "trivial-assertion": "high", "empty-test": "high", "swallowed-failure": "high",
    "skipped-without-reason": "medium", "sleep-in-test": "medium", "duplicate-test": "medium", "mock-only": "medium",
    "snapshot-only": "medium", "no-production-reference": "medium", "smoke-only": "medium",
    "debug-output": "low", "vague-name": "low",
}
SEVERITIES = ("low", "medium", "high")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}

_EXT = {".py": "py", ".js": "js", ".jsx": "jsx", ".mjs": "js", ".cjs": "js", ".ts": "ts", ".tsx": "jsx",
        ".mts": "ts", ".cts": "ts", ".java": "java", ".kt": "kotlin", ".go": "go", ".php": "php"}
SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "target", "coverage", "__pycache__", "venv", "site-packages",
             "fixtures", "__fixtures__", "testdata", "__snapshots__", "bower_components"}
MAX_BYTES = 1_500_000
_STRICT = bool(os.environ.get("DEVKIT_TESTAUDIT_STRICT"))  # re-raise analyzer errors (development)


# --------------------------------------------------------------------------- test files

def _lang(path) -> str | None:
    name = str(path)
    if name.endswith(".d.ts"):
        return None
    return _EXT.get(os.path.splitext(name)[1].lower())


_JS_TEST_FILE = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")
_JVM_TEST_FILE = re.compile(r"(?:Tests?|IT|ITCase|TestCase|Spec)\.(?:java|kt)$|^Test[A-Z]\w*\.(?:java|kt)$")


def _local_is_test_file(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    name, dirs = parts[-1], [d for d in parts[:-1] if d not in ("", ".", "..")]
    lang = _lang(name)
    if not lang or any(d in SKIP_DIRS or d.startswith(".") for d in dirs):
        return False
    if lang == "py":
        return name.startswith("test_") or name.endswith("_test.py") or name == "tests.py"
    if lang in ("js", "jsx", "ts"):
        return bool(_JS_TEST_FILE.search(name)) or "__tests__" in dirs or bool(
            dirs and dirs[-1] in ("test", "tests", "spec", "e2e"))
    if lang in ("java", "kotlin"):
        return bool(_JVM_TEST_FILE.search(name)) or "/src/test/" in "/" + "/".join(dirs) + "/"
    if lang == "go":
        return name.endswith("_test.go")
    return name.endswith("Test.php") or "tests" in (d.lower() for d in dirs)


def _testx():
    try:
        from . import testx  # `dev test` (optional; written separately)
        return testx
    except Exception:
        return None


def is_test_file(path) -> bool:
    """A runnable test file in a language the audit reads (test_*.py, *.test.ts, *Test.java, *_test.go, *Test.php,
    files under __tests__/ or src/test/). testx.is_test_file also counts helpers and data, which hold no tests."""
    return _local_is_test_file(str(path).replace(os.sep, "/"))


def _walk(root: Path):
    for d, subdirs, names in os.walk(root):
        subdirs[:] = sorted(s for s in subdirs if s not in SKIP_DIRS and not s.startswith("."))
        for name in sorted(names):
            p = Path(d) / name
            if is_test_file(p.relative_to(root).as_posix()):
                yield p


def changed_test_files(top, since=None) -> list:
    """Test files changed against the merge-base with origin/<default> (or `since`), plus staged, unstaged and
    untracked files. Paths relative to `top`, sorted; deleted files are left out."""
    from . import core
    top = Path(top).resolve()
    root = core.gitout(top, "rev-parse", "--show-toplevel")
    if not root:
        return []
    root = Path(root).resolve()
    names = None
    tx = _testx()
    if tx is not None and hasattr(tx, "changed_files"):
        try:
            names = {str(n) for n in tx.changed_files(root, since) or []}
        except Exception:
            names = None
    if names is None:
        names = _changed_files(root, since)
    out = set()
    for n in names:
        p = root / n
        try:
            rel = p.relative_to(top).as_posix()
        except ValueError:
            continue
        if p.is_file() and is_test_file(rel):
            out.add(rel)
    return sorted(out)


def _changed_files(root: Path, since=None) -> set:
    """Changed files relative to the repository top (the fallback when testx is not there)."""
    from . import core
    names = set()

    def add(out):
        names.update(x for x in out.split("\0") if x)

    base = _merge_base(root, since)
    if base:
        add(core.gitout(root, "diff", "--name-only", "-z", "--no-renames", base))
    else:
        add(core.gitout(root, "diff", "--name-only", "-z", "--no-renames"))
        add(core.gitout(root, "diff", "--cached", "--name-only", "-z", "--no-renames"))
    add(core.gitout(root, "ls-files", "--others", "--exclude-standard", "--full-name", "-z"))
    return names


def _merge_base(top: Path, since):
    from . import core
    ref = since
    if not ref:
        ref = core.gitout(top, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        if not ref:
            ref = next((f"origin/{b}" for b in ("main", "master", "develop", "trunk")
                        if core.git(top, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{b}")[0] == 0), "")
        if not ref:
            return None
    mb = core.gitout(top, "merge-base", ref, "HEAD")
    if mb:
        return mb
    return core.gitout(top, "rev-parse", "--verify", "-q", ref + "^{commit}") if since else None


# --------------------------------------------------------------------------- shared model

@dataclass
class _T:
    """One test as the analyzers see it; _judge turns it into findings."""
    name: str
    line: int
    bare: str = ""                 # the name without class, for vague-name
    scope: str = ""                # class or describe block, for duplicate-test
    skipped: bool = False          # unconditionally skipped: the body rules do not apply
    skip_line: int = 0             # > 0: skipped with no reason, reported at this line
    skip_fix: str = ""
    body: bool = True              # the body is known (not a reference to a function elsewhere)
    empty: bool = False
    kinds: list = field(default_factory=list)   # one per assertion: real | trivial | mock | snapshot
    trivial: str = ""
    implicit: bool = False         # can fail without an assertion we see (e2e steps, property tests, skips, ...)
    smoke: bool = False            # only says it checks for crashes ("no error", a ticket number): visible, not blocking
    swallowed: list = field(default_factory=list)
    sleeps: list = field(default_factory=list)
    prints: list = field(default_factory=list)
    dup: str = ""
    subject: str = ""


_VAGUE = {"", "it", "it works", "works", "work", "works correctly", "works fine", "work correctly", "should work",
          "should work correctly", "it should work", "is working", "something", "does something", "do something",
          "stuff", "thing", "things", "foo", "bar", "baz", "foobar", "test", "tests", "todo", "placeholder",
          "dummy", "tmp", "temp", "example", "sample", "ok", "okay", "a", "b", "c", "x", "y", "z", "case", "new",
          "my test", "first test", "a test", "basic test", "simple test"}
_VAGUE_NUM = re.compile(r"^(?:test|case|it|example|sample|scenario)?\s*\d+$")


def _vague(t: _T, lang: str, body_text: str = "") -> bool:
    raw = t.bare or t.name
    if lang in ("js", "jsx", "ts") or (lang == "php" and " " in raw):
        words = raw.strip().lower()
    else:
        s = re.sub(r"^(?:test_?|Test_?)", "", raw) if raw.lower().startswith("test") else raw
        s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s).replace("_", " ")
        words = " ".join(s.lower().split())
    words = re.sub(r"[.!]+$", "", words)
    if _VAGUE_NUM.match(words):
        return True
    if words not in _VAGUE:
        return False
    if body_text and lang not in ("js", "jsx", "ts"):
        rest = re.sub(r"^(?:test_?|Test_?)", "", raw)
        if rest and not rest.isdigit() and re.search(r"(?<![\w$])" + re.escape(rest) + r"(?![\w$])", body_text,
                                                     re.I):
            return False  # TestFoo that calls Foo(), test_c_ that uses np.c_: named after what it tests
    return True


_FIX_SKIP = {"py": 'give the reason: @pytest.mark.skip(reason="why, and the ticket") or @unittest.skip("why")',
             "js": "put the reason and a ticket in a comment on the .skip line, or fix and re-enable the test",
             "java": 'give the reason: @Disabled("why, and the ticket")',
             "go": 'give the reason: t.Skip("why, and the ticket")',
             "php": "give the reason: markTestSkipped('why, and the ticket') or ->skip('why')"}
_FIX_SWALLOW = {"py": "remove the try/except around the assertion or re-raise; to expect an error use pytest.raises "
                      "or self.assertRaises",
                "js": "remove the try/catch around the assertion or rethrow; to expect an error use "
                      "expect(...).toThrow() or .rejects",
                "java": "catch only the exception you expect, or use assertThrows / assertFailsWith",
                "go": "let the failure reach the test",
                "php": "remove the try/catch around the assertion or rethrow; to expect an error use "
                       "$this->expectException()"}
_FIX_SLEEP = {"py": "poll for the condition with a deadline, or use a fake clock (freezegun, time-machine)",
              "js": "wait for the condition (waitFor, expect.poll) or use fake timers",
              "java": "wait for the condition with a timeout (Awaitility) or inject a Clock",
              "go": "wait on a channel or a condition with a deadline (require.Eventually), or inject a clock",
              "php": "wait for the condition with a timeout, or fake the clock (Sleep::fake, Carbon::setTestNow)"}


def _family(lang):
    return {"jsx": "js", "ts": "js", "kotlin": "java"}.get(lang, lang)


def _finding(rel, line, test, rule, message, fix):
    return {"file": rel, "line": int(line or 1), "test": test, "rule": rule, "severity": RULES[rule],
            "message": message, "fix": fix}


def _judge(rel: str, lang: str, tests: list, extra: list, bodies: dict) -> list:
    fam = _family(lang)
    out = []
    for t in tests:
        subj = f"{t.subject}()" if t.subject else ""
        if t.skip_line:
            out.append(_finding(rel, t.skip_line, t.name, "skipped-without-reason", "is skipped with no reason",
                                t.skip_fix or _FIX_SKIP[fam]))
        if _vague(t, lang, bodies.get(id(t), "")):
            eg = {"py": "test_parse_rejects_empty_input", "js": "'rejects an empty input'",
                  "java": "rejectsAnEmptyInput", "go": "TestParseRejectsEmptyInput",
                  "php": "test_rejects_an_empty_input"}[fam]
            out.append(_finding(rel, t.line, t.name, "vague-name",
                                f"has a name that says nothing about the behavior: {t.bare or t.name!r}",
                                f"rename it to the behavior and the case it checks, for example {eg}"))
        if t.skipped or not t.body:
            continue
        if t.empty:
            out.append(_finding(rel, t.line, t.name, "empty-test", "has an empty body, so it always passes",
                                "write the check (call the code, assert the result) or delete the test"))
            continue
        for line, desc in t.swallowed[:1]:
            out.append(_finding(rel, line, t.name, "swallowed-failure",
                                f"catches its own assertion failure ({desc}) and carries on, so that assertion can never fail",
                                _FIX_SWALLOW[fam]))
        kinds = t.kinds
        if not kinds:
            if t.smoke:
                out.append(_finding(
                    rel, t.line, t.name, "smoke-only",
                    "only checks that the code does not crash (its name or comments say so)",
                    "assert the outcome too (the returned value, the state, or the error), so a wrong result fails"))
            elif not t.implicit:
                out.append(_finding(
                    rel, t.line, t.name, "no-assertion", "asserts nothing, so it passes whatever the code does",
                    f"assert the value {subj} returns for this input, or delete the test" if subj else
                    "assert the result or the state the code under test produces, or delete the test"))
        elif all(k == "trivial" for k in kinds):
            out.append(_finding(
                rel, t.line, t.name, "trivial-assertion", f"its only assertion is always true: {t.trivial}",
                f"compare what {subj} returns with a value worked out by hand, or delete the test" if subj else
                "compare the real result with a value worked out by hand, or delete the test"))
        elif all(k == "snapshot" for k in kinds):
            out.append(_finding(rel, t.line, t.name, "snapshot-only",
                                "only compares with a stored snapshot, so whatever was recorded first passes",
                                "also assert the values that matter, worked out by hand, or review the snapshot"))
        elif "mock" in kinds and all(k in ("mock", "trivial") for k in kinds):
            out.append(_finding(
                rel, t.line, t.name, "mock-only", "only checks mocks; nothing checks the real result or state",
                (f"assert what {subj} returns or changes" if subj else "assert the result or state the code under "
                 "test produces") + "; keep a mock check only when the call itself is the contract"))
        for line, call in t.sleeps[:1]:
            out.append(_finding(rel, line, t.name, "sleep-in-test",
                                f"waits with {call}, which makes the test slow and flaky", _FIX_SLEEP[fam]))
        for line, call in t.prints[:1]:
            out.append(_finding(rel, line, t.name, "debug-output", f"prints debug output ({call})",
                                "remove it, or turn what you wanted to see into an assertion"))
    first = {}
    for t in tests:
        if not t.dup or t.skipped or not t.body or t.empty:
            continue
        key = (t.scope, t.dup)
        if key in first:
            o = first[key]
            out.append(_finding(rel, t.line, t.name, "duplicate-test",
                                f"has the same body as {o.name} (line {o.line})",
                                "delete one, or make it check a different case (parametrize if you need both)"))
        else:
            first[key] = t
    return out + extra


_SMOKE_DOC = re.compile(
    r"(?i)(?<![\w#])#\d{3,}\b|\b(?:crash\w*|segfault\w*|seg\s?fault|hang|hangs|hung|deadlock\w*|leaks?|smoke|"
    r"used\s+to\s+(?:fail|raise|crash|error|segfault|hang|throw|break|be\s+limited)|"
    r"none\s+of\s+(?:these|them|this|the\s+\w+)\s+should|(?:should|must)\s+(?:\w+\s+)?work|"
    r"(?:does|do|should|must|will|can)(?:\s+not|n'?t)\s+(?:error|fail|raise|crash|throw|warn|segfault|hang|blow)\w*|"
    r"(?:raises?|throws?|errors?(?:\s+out)?|fails?|exits?|aborts?)\s+(?:if|when|unless|on|for)\b|"
    r"no\s+(?:errors?|exceptions?|warnings?|crash(?:es)?)\b|without\s+(?:an?\s+)?(?:error|exception|crash|raising|warning)|"
    r"(?:should|must)\s+(?:succeed|work|pass|run|not\s+be\s+raised)|runs?\s+at\s+all|"
    r"regression\s+(?:test|for)|(?:ticket|trac|issue|bug|gh)\s*-?#?\s*\d+)")
_SMOKE_NAME = re.compile(r"(?i)\b(?:crash\w*|segfault\w*|hangs?|deadlock\w*|smoke|"
                         r"(?:does|do|should|must)\s+not\s+(?:raise|crash|fail|error|throw|warn|hang)|"
                         r"no\s+(?:error|exception|crash|warning|raise)s?|without\s+(?:error|exception|crash|raising)|"
                         r"(?:gh|issue|bug)\s?\d+)\b|^regression\b")


def _smoke(name: str, doc: str) -> bool:
    """Does the test say it checks that the code does not raise or crash (a deliberate test without an assertion)?"""
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", re.sub(r"^(?:test_?|Test_?)", "", name)).replace("_", " ")
    return bool(_SMOKE_NAME.search(words.strip()) or (doc and _SMOKE_DOC.search(doc)))


def _no_prod(rel, line):
    return _finding(rel, line, "", "no-production-reference",
                    "uses only the standard library and the test framework; it never touches project code",
                    "import the code under test and assert on its behavior, or delete the file")


def _clip(text: str, n: int = 60) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


_SUPPORT = re.compile(r"(?i)(?:^|[./_\\-])(?:tests?|testing|testutils?|test_?helpers?|helpers?|utils?|support|fixtures?|"
                      r"conftest|common|harness|mixins?|base|shared|assertions?|checks?|matchers?|scenarios?|spec|"
                      r"specs|setup|__tests__|__mocks__|mocks?|testlib)(?:$|[./_\\-])")


# --------------------------------------------------------------------------- Python

def _stdlib() -> set:
    names = set(getattr(sys, "stdlib_module_names", ()) or ())
    if names:
        return names
    import sysconfig
    names = set(sys.builtin_module_names)
    std = Path(sysconfig.get_paths().get("stdlib") or "")
    for d in (std, std / "lib-dynload"):
        try:
            for p in d.iterdir():
                if p.suffix in (".py", ".so") or (p.is_dir() and (p / "__init__.py").exists()):
                    names.add(p.name.split(".")[0])
        except OSError:
            pass
    names.discard("site-packages")
    return names


_STD = None
_PY_FRAMEWORKS = {"unittest", "pytest", "_pytest", "mock", "hypothesis", "parameterized", "nose", "nose2",
                  "testtools", "pytest_asyncio", "freezegun", "time_machine", "pytest_mock", "ddt", "sure", "expects",
                  "assertpy", "hamcrest"}
_PY_UNSURE_MODS = {"os", "sys", "subprocess", "importlib", "runpy", "urllib", "http", "socket", "ctypes", "pkgutil",
                   "doctest", "glob", "shutil", "pathlib", "multiprocessing", "sysconfig", "site", "zipimport",
                   "webbrowser", "ftplib", "smtplib", "xmlrpc", "ssl", "inspect", "imp", "code", "zipapp"}
_PY_UNSURE_NAMES = {"open", "__file__", "__import__", "exec", "eval", "compile", "execfile", "Path", "globals",
                    "getfixturevalue", "__builtins__", "input"}
_PY_FIXTURES = {"self", "cls", "tmp_path", "tmpdir", "tmp_path_factory", "tmpdir_factory", "capsys", "capfd",
                "capsysbinary", "capfdbinary", "caplog", "monkeypatch", "recwarn", "record_property",
                "record_testsuite_property", "record_xml_attribute", "cache", "doctest_namespace", "pytestconfig",
                "subtests", "mocker", "request"}
_PY_MOCK_FACTORIES = {"Mock", "MagicMock", "AsyncMock", "NonCallableMock", "NonCallableMagicMock", "create_autospec",
                      "PropertyMock"}
_PY_MOCK_ASSERT = re.compile(r"^assert_(?:called|not_called|any_call|has_calls|awaited|not_awaited|any_await|has_awaits)")
_PY_MOCK_ATTRS = {"called", "call_count", "call_args", "call_args_list", "mock_calls", "method_calls", "await_count",
                  "await_args", "await_args_list", "awaited"}
_PY_ASSERTISH = re.compile(r"^_*(?:assert|check|expect|verify|should|validate|ensure|require)", re.I)
_PY_ASSERT_EXACT = {"fail", "raises", "warns", "deprecated_call", "raise_for_status", "testmod", "testfile",
                    "run_docstring_examples", "assert_"}
_PY_TESTCASE = {"setUp", "tearDown", "setUpClass", "tearDownClass", "asyncSetUp", "asyncTearDown", "subTest",
                "addCleanup", "addAsyncCleanup", "doCleanups", "id", "shortDescription", "skipTest", "run", "debug",
                "countTestCases", "defaultTestResult", "enterContext", "enterAsyncContext", "addTypeEqualityFunc"}
_PY_TC_BASES = {"TestCase", "IsolatedAsyncioTestCase", "object", "SimpleTestCase", "TransactionTestCase"}
_PY_BUILTINS = set(dir(__import__("builtins")))
_PY_NOT_SUBJECT = _PY_BUILTINS | {
    "print", "pprint", "subTest", "patch", "object", "sleep", "skipTest", "skip", "fixture", "parametrize", "mark",
    "approx", "setUp", "Mock", "MagicMock", "AsyncMock", "mock", "create_autospec", "dumps", "loads", "join", "format",
    "append", "get", "items", "keys", "values", "split", "strip", "encode", "decode", "monotonic", "time"}
_UT_TRUE, _UT_FALSE = {"assertTrue", "assert_", "failUnless"}, {"assertFalse", "failIf"}
_UT_EQ = {"assertEqual", "assertEquals", "assertIs", "assertListEqual", "assertTupleEqual", "assertDictEqual",
          "assertSetEqual", "assertSequenceEqual", "assertMultiLineEqual", "assertCountEqual", "assertAlmostEqual",
          "assertAlmostEquals"}
_UT_NE = {"assertNotEqual", "assertNotEquals", "assertIsNot"}


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        parts.append(_dotted(node.func) + "()")
    else:
        return ""
    return ".".join(reversed(parts))


def _callname(call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""


_LITERAL_NODES = (ast.Constant, ast.Tuple, ast.List, ast.Set, ast.Dict, ast.UnaryOp, ast.BinOp)


def _const(n):
    if isinstance(n, ast.Constant):
        return True, n.value
    if not isinstance(n, _LITERAL_NODES):
        return False, None
    try:
        return True, ast.literal_eval(n)
    except Exception:
        return False, None


def _seg(lines, node) -> str:
    """Source text of a node (ast.get_source_segment re-splits the whole file on every call)."""
    try:
        l0, l1, c0, c1 = node.lineno - 1, node.end_lineno - 1, node.col_offset, node.end_col_offset
    except AttributeError:
        return ""
    if l0 >= len(lines) or l1 >= len(lines):
        return ""
    if l0 == l1:
        return lines[l0].encode()[c0:c1].decode("utf-8", "replace")
    return (lines[l0].encode()[c0:].decode("utf-8", "replace") + "".join(lines[l0 + 1:l1]) +
            lines[l1].encode()[:c1].decode("utf-8", "replace"))


def _nocall(n) -> bool:
    return not any(isinstance(x, (ast.Call, ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr, ast.Lambda))
                   for x in ast.walk(n))


def _same(a, b) -> bool:
    return _nocall(a) and ast.dump(a) == ast.dump(b)


_CMP = {ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Lt: lambda a, b: a < b,
        ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
        ast.Is: lambda a, b: a == b, ast.IsNot: lambda a, b: a != b, ast.In: lambda a, b: a in b,
        ast.NotIn: lambda a, b: a not in b}


def _py_always_true(e) -> bool:
    ok, v = _const(e)
    if ok:
        return bool(v)
    if isinstance(e, ast.UnaryOp) and isinstance(e.op, ast.Not):
        ok, v = _const(e.operand)
        return ok and not v
    if isinstance(e, ast.Compare) and len(e.ops) == 1:
        a, op, b = e.left, e.ops[0], e.comparators[0]
        oka, va = _const(a)
        okb, vb = _const(b)
        if oka and okb:
            try:
                return bool(_CMP[type(op)](va, vb))
            except Exception:
                return False
        return _same(a, b) and isinstance(op, (ast.Eq, ast.Is, ast.LtE, ast.GtE))
    return False


def _py_trivial_call(name, call) -> bool:
    args = call.args
    if name in _UT_TRUE and args:
        return _py_always_true(args[0])
    if name in _UT_FALSE and args:
        ok, v = _const(args[0])
        return ok and not v
    if len(args) >= 2 and (name in _UT_EQ or name in _UT_NE):
        oka, va = _const(args[0])
        okb, vb = _const(args[1])
        if oka and okb:
            return (va == vb) == (name in _UT_EQ)
        return name in _UT_EQ and _same(args[0], args[1])
    if args and name in ("assertIsNone", "assertIsNotNone"):
        ok, v = _const(args[0])
        return ok and ((v is None) == (name == "assertIsNone"))
    return False


_AST, _CTX = ast.AST, ast.expr_context
_TRY_STAR = getattr(ast, "TryStar", ast.Try)
_LOOPS = (ast.For, ast.AsyncFor, ast.While)


def _kids(n):
    out = []
    for f in n._fields:
        v = getattr(n, f, None)
        if v.__class__ is list:
            out.extend(x for x in v if isinstance(x, _AST) and not isinstance(x, _CTX))
        elif isinstance(v, _AST) and not isinstance(v, _CTX):
            out.append(v)
    return out


_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _walk_ctx(stmts, loop=False):
    """(node, flags) for every node under the statements, in source order (faster than ast.walk).
    flags: 1 inside a loop, 2 inside a nested function."""
    out = []
    stack = [(s, 1 if loop else 0) for s in reversed(stmts)]
    while stack:
        n, fl = stack.pop()
        out.append((n, fl))
        inner = fl | (1 if isinstance(n, _LOOPS) else 0) | (2 if isinstance(n, _NESTED) else 0)
        kids = _kids(n)
        for c in reversed(kids):
            stack.append((c, inner))
    return out


def _walk_fast(root):
    """Every node, except the bodies of test functions (each test walks its own body once)."""
    out, stack = [], [root]
    while stack:
        n = stack.pop()
        out.append(n)
        if n.__class__ in (ast.FunctionDef, ast.AsyncFunctionDef) and n.name.startswith("test"):
            stack.extend(n.decorator_list)
            continue
        stack.extend(_kids(n))
    return out


def _walk_flat(stmts):
    """Nodes under the statements without entering nested functions, lambdas or classes."""
    stack = list(stmts)
    while stack:
        n = stack.pop()
        yield n
        for c in ast.iter_child_nodes(n):
            if not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                stack.append(c)


def _strip_doc(body):
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def _is_patch(call) -> bool:
    return isinstance(call, ast.Call) and _dotted(call.func) in (
        "patch", "mock.patch", "unittest.mock.patch", "patch.object", "mock.patch.object",
        "unittest.mock.patch.object", "patch.multiple", "mock.patch.multiple")


_BROAD_PY = {"Exception", "BaseException", "AssertionError"}


def _py_broad(t) -> str:
    if t is None:
        return "a bare except"
    if isinstance(t, ast.Tuple):
        return next((x for x in (_py_broad(e) for e in t.elts) if x), "")
    name = t.attr if isinstance(t, ast.Attribute) else t.id if isinstance(t, ast.Name) else ""
    return f"except {name}" if name in _BROAD_PY else ""


def _py_swallowing(body) -> bool:
    for s in body:
        if isinstance(s, (ast.Pass, ast.Continue, ast.Break)):
            continue
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant):
            continue
        if isinstance(s, ast.Return) and (s.value is None or isinstance(s.value, ast.Constant)):
            continue
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
            d = _dotted(s.value.func)
            if d in ("print", "pprint", "pprint.pprint", "warnings.warn", "traceback.print_exc",
                     "sys.stderr.write") or re.match(r"^(?:logging|logger|log|LOG|LOGGER|self\.log\w*)\.\w+$", d):
                continue
        return False
    return True


class _PyFile:
    def __init__(self, tree, text, rel):
        global _STD
        if _STD is None:
            _STD = _stdlib()
        self.tree, self.text, self.rel = tree, text, rel
        self.lines = [ln + "\n" for ln in text.split("\n")]
        self.funcs, self.classes, self.imports = {}, {}, {}
        self.file_mocks, self.mods, self.unsure = set(), set(), False
        self._methods = {}
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs[n.name] = n
            elif isinstance(n, ast.ClassDef):
                self.classes[n.name] = n
        for n in _walk_fast(tree):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                self.add_import(n)
            elif isinstance(n, ast.Assign):
                if _py_is_mock_factory(n.value):
                    for tg in n.targets:
                        k = _py_key(tg)
                        if k and k.startswith("self."):
                            self.file_mocks.add(k)
            elif isinstance(n, ast.Name):
                if n.id in _PY_UNSURE_NAMES:
                    self.unsure = True
            elif isinstance(n, ast.Attribute):
                if n.attr in ("__file__", "getfixturevalue"):
                    self.unsure = True
        self.sleep_names = {k for k, v in self.imports.items() if v in ("time.sleep",)}
        self._tests = None
        self.sleep_patched = bool(re.search(r"""["']time\.sleep["']|setattr\(\s*time\s*,\s*["']sleep""", text))
        self._memo = {}

    def add_import(self, n):
        if isinstance(n, ast.Import):
            for a in n.names:
                self.imports.setdefault(a.asname or a.name.split(".")[0],
                                        a.name if a.asname else a.name.split(".")[0])
                self.mods.add(a.name.split(".")[0])
        else:
            mod = "." * n.level + (n.module or "")
            for a in n.names:
                self.imports.setdefault(a.asname or a.name, f"{mod}.{a.name}")
            self.mods.add(mod.split(".")[0] if not n.level else ".")

    # -- helpers that assert
    def methods(self, cls, seen=None):
        """Methods of a class and its in-file bases; None when a base is unknown (defined elsewhere)."""
        if not seen and id(cls) in self._methods:
            return self._methods[id(cls)]
        top = not seen
        seen = seen or set()
        out, unknown = {}, False
        for b in cls.bases:
            bn = b.attr if isinstance(b, ast.Attribute) else b.id if isinstance(b, ast.Name) else ""
            if bn in self.classes and bn not in seen:
                seen.add(bn)
                sub, unk = self.methods(self.classes[bn], seen)
                out.update(sub)
                unknown = unknown or unk
            elif bn not in _PY_TC_BASES:
                unknown = True
        out.update({n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))})
        if top:
            self._methods[id(cls)] = (out, unknown)
        return out, unknown

    def asserts(self, fn, cls, depth=0) -> bool:
        """Does a helper assert (directly, or through another helper), or say it checks for crashes?"""
        key = id(fn)
        if key in self._memo:
            return self._memo[key]
        self._memo[key] = False
        doc = fn.body[0].value.value if _strip_doc(fn.body) is not fn.body else ""
        found = _smoke(fn.name, doc + " " + " ".join(re.findall(r"#[^\n]*", _seg(self.lines, fn))))
        for n, _ in ([] if found else _walk_ctx(fn.body)):
            if isinstance(n, (ast.Assert, ast.Raise)):
                found = True
            elif isinstance(n, ast.Call):
                name = _callname(n)
                if _PY_ASSERTISH.match(name) or name in _PY_ASSERT_EXACT or _py_checked(n):
                    found = True
                elif depth < 6 and self.helper_call(n, cls, depth + 1) in ("asserts", "unknown"):
                    found = True
            if found:
                break
        self._memo[key] = found
        return found

    def support(self, name) -> bool:
        """Was `name` imported from a test-support module (helpers, utils, conftest, a relative import)?"""
        mod = self.imports.get(name, "")
        if not mod:
            return False
        if mod.startswith("."):
            return True
        top = mod.split(".")[0]
        return top not in _STD and top not in _PY_FRAMEWORKS and bool(_SUPPORT.search(mod))

    def helper_call(self, call, cls, depth=0, params=()) -> str:
        """asserts | unknown | '' for a call inside a test: does it reach an assertion we cannot see?"""
        f = call.func
        if isinstance(f, ast.Name):
            if f.id in self.funcs:
                return "asserts" if self.asserts(self.funcs[f.id], None, depth) else ""
            if f.id in params:
                return "unknown"
            return "unknown" if self.support(f.id) else ""
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            root = f.value.id
            if root in ("self", "cls") and cls is not None:
                meths, unknown = self.methods(cls)
                if f.attr in meths:
                    return "asserts" if self.asserts(meths[f.attr], cls, depth) else ""
                if f.attr in _PY_TESTCASE:
                    return ""
                return "unknown" if unknown else ""
            if self.support(root):
                return "unknown"
            if root in params and root not in _PY_FIXTURES:
                return "unknown"
        return ""

    # -- tests
    def tests(self):
        out = []

        def block(stmts, cls, skipped):
            for s in stmts:
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if s.name.startswith("test") and not any(
                            _dotted(d.func if isinstance(d, ast.Call) else d).endswith("fixture")
                            for d in s.decorator_list):
                        out.append((s, cls, skipped))
                elif isinstance(s, ast.ClassDef):
                    if s.name.startswith("Test") or s.name.endswith(("Test", "Tests", "TestCase")) or s.bases:
                        sk = skipped or self.class_skip(s)
                        block(s.body, s, sk)
                elif isinstance(s, ast.If):
                    block(s.body, cls, skipped)
                    block(s.orelse, cls, skipped)
                elif isinstance(s, (ast.Try, ast.With)):
                    block(s.body, cls, skipped)

        self.class_findings = []
        block(self.tree.body, None, False)
        return out

    def class_skip(self, c) -> bool:
        for d in c.decorator_list:
            r = _py_skip_deco(d)
            if r:
                if r == "noreason":
                    self.class_findings.append((d.lineno, c.name))
                return True
        return False


def _py_skip_deco(d):
    target = d.func if isinstance(d, ast.Call) else d
    if _dotted(target) not in ("skip", "unittest.skip", "pytest.mark.skip", "mark.skip"):
        return None
    if not isinstance(d, ast.Call):
        return "noreason"
    reason = d.args[0] if d.args else next((k.value for k in d.keywords if k.arg == "reason"), None)
    if reason is None or (isinstance(reason, ast.Constant) and isinstance(reason.value, str)
                          and not reason.value.strip()):
        return "noreason"
    return "reason"


def _py_skip_call(stmt):
    """'reason' | 'noreason' | None for an unconditional skip statement."""
    call = None
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        call = stmt.value
        if _dotted(call.func) not in ("self.skipTest", "pytest.skip", "skipTest", "skip", "unittest.skip"):
            return None
    elif isinstance(stmt, ast.Raise) and stmt.exc is not None:
        exc = stmt.exc
        name = _dotted(exc.func if isinstance(exc, ast.Call) else exc)
        if not name.endswith("SkipTest"):
            return None
        if not isinstance(exc, ast.Call):
            return "noreason"
        call = exc
    else:
        return None
    reason = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg in ("reason", "msg")), None)
    if reason is None or (isinstance(reason, ast.Constant) and isinstance(reason.value, str)
                          and not reason.value.strip()):
        return "noreason"
    return "reason"


def _py_roots(nodes, mocks) -> tuple:
    """(root names, touches a mock attribute) for the expressions of one assertion."""
    roots, mock_attr = set(), False
    for e in nodes:
        for n in ast.walk(e):
            if isinstance(n, ast.Attribute):
                if n.attr in _PY_MOCK_ATTRS:
                    mock_attr = True
                if isinstance(n.value, ast.Name) and n.value.id in ("self", "cls"):
                    roots.add(f"self.{n.attr}")
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in ("self", "cls"):
                if n.id in _PY_BUILTINS and n.id not in mocks:
                    continue
                roots.add(n.id)
    # self.x.y: keep only self.x (drop a bare "self" use)
    return roots, mock_attr


def _py_key(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in ("self", "cls"):
        return f"self.{node.attr}"
    return None


def _py_root(node):
    while True:
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Attribute):
            k = _py_key(node)
            if k and k.startswith("self."):
                return k
            node = node.value
        elif isinstance(node, (ast.Subscript, ast.Await)):
            node = node.value
        elif isinstance(node, ast.Name):
            return node.id
        else:
            return None


def _py_is_mock_factory(call) -> bool:
    if not isinstance(call, ast.Call):
        return False
    d = _dotted(call.func)
    last = d.split(".")[-1]
    if last in _PY_MOCK_FACTORIES or d.startswith("mocker."):
        return True
    return last == "start" and isinstance(call.func, ast.Attribute) and _is_patch(call.func.value)


def _analyze_py(text, path, base, rel):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return [], []
    pf = _PyFile(tree, text, rel)
    name = path.name
    if not (name.startswith("test_") or name.endswith("_test.py")) and not (
            pf.mods & {"unittest", "pytest", "django", "nose", "nose2", "testtools", "hypothesis", "twisted"}
            or any(_dotted(b).split(".")[-1].endswith(("TestCase", "Test")) for c in pf.classes.values()
                   for b in c.bases)):
        return [], []  # jinja2/tests.py and friends: functions named test_* that are not tests
    file_mocks = pf.file_mocks
    tests, bodies = [], {}
    found = pf.tests()
    for fn, cls, cls_skipped in found:
        t = _py_test(pf, fn, cls, cls_skipped, file_mocks)
        tests.append(t)
        bodies[id(t)] = _seg(pf.lines, fn)
    extra = [_finding(rel, line, name, "skipped-without-reason", "is skipped with no reason", _FIX_SKIP["py"])
             for line, name in pf.class_findings]
    if tests and _py_no_prod(pf, [x[0] for x in found]):
        extra.append(_no_prod(rel, tests[0].line))
    return tests, extra, bodies


def _py_test(pf: _PyFile, fn, cls, cls_skipped, file_mocks) -> _T:
    t = _T(name=f"{cls.name}.{fn.name}" if cls else fn.name, line=fn.lineno, bare=fn.name,
           scope=cls.name if cls else "", skipped=cls_skipped)
    a = fn.args
    params = [x.arg for x in getattr(a, "posonlyargs", []) + a.args + a.kwonlyargs]
    params += [x.arg for x in (a.vararg, a.kwarg) if x]
    for d in fn.decorator_list:
        r = _py_skip_deco(d)
        if r:
            t.skipped = True
            if r == "noreason":
                t.skip_line = d.lineno
        if _dotted(d.func if isinstance(d, ast.Call) else d).split(".")[-1] in ("given", "settings", "example"):
            t.implicit = True
    body = _strip_doc(fn.body)
    if all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
           for s in body):
        t.empty = True
        return t
    for s in body:
        r = _py_skip_call(s)
        if r:
            t.skipped = True
            if r == "noreason":
                t.skip_line = s.lineno
            break
    if all(isinstance(s, (ast.Import, ast.ImportFrom)) for s in body):
        t.implicit = True
    doc = fn.body[0].value.value if fn.body is not body and fn.body else ""
    if _smoke(fn.name, doc + " " + " ".join(re.findall(r"#[^\n]*", _seg(pf.lines, fn)))):
        t.implicit = t.smoke = True
    # mocks: patch decorators inject the first parameters after self
    mocks = set(file_mocks)
    injected = 0
    for d in fn.decorator_list:
        if _is_patch(d) and _dotted(d.func) != "patch.dict":
            given_new = any(k.arg in ("new", "new_callable") for k in d.keywords) or \
                len(d.args) >= (3 if _dotted(d.func).endswith("object") else 2)
            injected += 0 if given_new else 1
    rest = [p for p in params if p not in ("self", "cls")]
    mocks.update(rest[:injected])
    events, assigns, guarded = [], [], []
    for n, lp in _walk_ctx(body):
        tn = n.__class__
        if tn is ast.Call or tn is ast.Assert or tn is ast.Raise or tn is ast.Yield or tn is ast.YieldFrom:
            events.append((n, lp))
        elif tn is ast.Assign or tn is ast.AnnAssign:
            assigns.append(n)
        elif tn is ast.With or tn is ast.AsyncWith or tn is ast.Try or tn is _TRY_STAR:
            guarded.append(n)
        elif tn is ast.Name:
            if n.id in _PY_UNSURE_NAMES:
                pf.unsure = True
        elif tn is ast.Attribute:
            if n.attr in ("__file__", "getfixturevalue"):
                pf.unsure = True
        elif tn is ast.Import or tn is ast.ImportFrom:
            pf.add_import(n)
    for n in assigns:
        if n.value is not None and _py_is_mock_factory(n.value):
            for tg in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                k = _py_key(tg)
                if k:
                    mocks.add(k)
    for n in guarded:
        if isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if (_is_patch(it.context_expr) or _py_is_mock_factory(it.context_expr)) and it.optional_vars:
                    k = _py_key(it.optional_vars)
                    if k:
                        mocks.add(k)
    derived = set()
    for n in assigns:
        if isinstance(n, ast.Assign) and _py_root(n.value) in (mocks | derived) and not _py_is_mock_factory(n.value):
            for tg in n.targets:
                k = _py_key(tg)
                if k:
                    derived.add(k)
    md = mocks | derived
    snaps = {p for p in params if "snapshot" in p.lower()}
    calls = []
    trivial_seen = ""
    for n, in_loop in events:
        if isinstance(n, (ast.Yield, ast.YieldFrom)):
            t.implicit = True
            continue
        if isinstance(n, ast.Assert):
            if _py_always_true(n.test):
                t.kinds.append("trivial")
                trivial_seen = trivial_seen or _clip(_seg(pf.lines, n) or "assert …")
                continue
            if snaps and {x.id for x in ast.walk(n.test) if isinstance(x, ast.Name)} & snaps:
                t.kinds.append("snapshot")
                continue
            roots, mattr = _py_roots([n.test], md)
            t.kinds.append("mock" if mattr or (roots and roots <= md) else "real")
        elif isinstance(n, ast.Raise):
            if not (n.exc is not None and _dotted(n.exc.func if isinstance(n.exc, ast.Call) else n.exc)
                    .endswith("SkipTest")):
                t.kinds.append("real")
        elif isinstance(n, ast.Call):
            name = _callname(n)
            dotted = _dotted(n.func)
            if name in ("skipTest", "skip") and dotted in ("self.skipTest", "pytest.skip"):
                t.implicit = True
                continue
            if _PY_ASSERTISH.match(name) or name in _PY_ASSERT_EXACT:
                if _PY_MOCK_ASSERT.match(name):
                    t.kinds.append("mock")
                elif "snapshot" in name.lower() or (name == "assert_match" and "snap" in dotted.lower()):
                    t.kinds.append("snapshot")
                elif _py_trivial_call(name, n):
                    t.kinds.append("trivial")
                    trivial_seen = trivial_seen or _clip(_seg(pf.lines, n) or name)
                elif name.startswith("assert") and isinstance(n.func, ast.Attribute) and \
                        isinstance(n.func.value, ast.Name) and n.func.value.id in ("self", "cls"):
                    roots, mattr = _py_roots(list(n.args) + [k.value for k in n.keywords], md)
                    t.kinds.append("mock" if mattr or (roots and roots <= md) else "real")
                else:
                    t.kinds.append("real")
                continue
            if _py_checked(n):
                t.kinds.append("real")
            h = pf.helper_call(n, cls, 0, params)
            if h:
                t.kinds.append("real")
            if (dotted in ("time.sleep",) or (isinstance(n.func, ast.Name) and n.func.id in pf.sleep_names)) \
                    and not in_loop and not pf.sleep_patched:
                ok, v = _const(n.args[0]) if n.args else (False, None)
                if not (ok and v == 0):
                    t.sleeps.append((n.lineno, dotted + "()"))
            elif dotted in ("asyncio.sleep", "trio.sleep", "anyio.sleep") and not in_loop and n.args:
                ok, v = _const(n.args[0])
                if ok and isinstance(v, (int, float)) and v > 0:
                    t.sleeps.append((n.lineno, dotted + "()"))
            if dotted in ("print", "pprint", "pprint.pprint") and not any(k.arg == "file" for k in n.keywords):
                t.prints.append((n.lineno, dotted + "()"))
            calls.append(n)
    t.trivial = trivial_seen
    # swallowed failures
    for n in guarded:
        if isinstance(n, (ast.Try, _TRY_STAR)):
            first = _py_first_assert(n.body)
            if first:
                for h in n.handlers:
                    desc = _py_broad(h.type)
                    if desc and _py_swallowing(h.body):
                        t.swallowed.append((first, desc))
                        break
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                c = it.context_expr
                if isinstance(c, ast.Call) and _dotted(c.func) in ("suppress", "contextlib.suppress") and \
                        any(_py_broad(a) for a in c.args):
                    first = _py_first_assert(n.body)
                    if first:
                        t.swallowed.append((first, "contextlib.suppress"))
    # the code under test: the last call that is not the framework
    for c in sorted(calls, key=lambda c: (c.lineno, c.col_offset), reverse=True):
        name = _callname(c)
        root = _py_root(c.func)
        if name and name not in _PY_NOT_SUBJECT and not name.startswith("_") and root not in md \
                and not _PY_ASSERTISH.match(name) and not _py_is_mock_factory(c) and not _is_patch(c):
            t.subject = name
            break
    # duplicates: decorators and body as text (without the docstring), whitespace collapsed
    src_body = "".join(_seg(pf.lines, s_) + "\n" for s_ in body)
    if not (len(body) == 1 and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Call)
            and not body[0].value.args and not body[0].value.keywords) and \
            not re.search(r"_testMethodName|self\.id\(\)|request\.node|request\.function", src_body):
        t.dup = "|".join(" ".join(_seg(pf.lines, d).split()) for d in fn.decorator_list) + "#" + " ".join(
            re.sub(r"(?m)#.*$", "", src_body).split())
    return t


def _py_checked(call) -> bool:
    """subprocess.run(..., check=True), also through **kwargs: a non-zero exit fails the test."""
    for k in call.keywords:
        if k.arg == "check" and isinstance(k.value, ast.Constant) and k.value.value is True:
            return True
        if k.arg is None and _callname(call) in ("run", "call", "check_call", "check_output", "Popen"):
            return True
    return False


def _py_first_assert(stmts) -> int:
    for n in _walk_flat(stmts):
        if isinstance(n, ast.Assert):
            return n.lineno
        if isinstance(n, ast.Call):
            name = _callname(n)
            if _PY_ASSERTISH.match(name) or name in ("fail",):
                return n.lineno
    return 0


def _py_no_prod(pf: _PyFile, fns) -> bool:
    mods = pf.mods
    if pf.unsure or "." in mods:
        return False
    for n in pf.classes.values():
        for b in n.bases:
            bn = b.attr if isinstance(b, ast.Attribute) else b.id if isinstance(b, ast.Name) else ""
            if bn not in _PY_TC_BASES and bn not in pf.classes:
                return False
    if any(m not in _STD and m not in _PY_FRAMEWORKS for m in mods) or mods & _PY_UNSURE_MODS:
        return False
    for fn in fns:
        a = fn.args
        if any(x.arg not in _PY_FIXTURES for x in getattr(a, "posonlyargs", []) + a.args + a.kwonlyargs):
            return False
        if any(_dotted(d.func if isinstance(d, ast.Call) else d).split(".")[-1] in ("parametrize", "usefixtures")
               for d in fn.decorator_list):
            return False
    return True


# --------------------------------------------------------------------------- the scanner for brace languages

_WSRE = re.compile(r"\s*")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_NOT_NL = re.compile(r"[^\n]")
_BRK = re.compile(r"[(){}\[\]]")
_LEX_RX = {"js": re.compile(r"[\"'`/{}]"), "jsx": re.compile(r"[\"'`/{}<]"), "java": re.compile(r"[\"'/]"),
           "kotlin": re.compile(r"[\"'/`{}]"), "go": re.compile(r"[\"'/`]"), "php": re.compile(r"[\"'/#<]")}
_REGEX_KW = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do", "else",
             "yield", "await", "and", "or", "not"}
_TPL = re.compile(r"[`\\]|\$\{")
_KSTR = re.compile(r"[\"\\\n]|\$\{")
_KRAW = re.compile(r"\"\"\"|\$\{")
_JTB = re.compile(r"\"\"\"|\\")
_HEREDOC = re.compile(r"<<<[ \t]*([\"']?)([A-Za-z_]\w*)\1\r?\n")
_QCACHE = {}


def _ws(s, i):
    return _WSRE.match(s, i).end()


class _Lexer:
    """Finds strings, template literals, regex literals, comments, JSX text and PHP heredocs, so brackets inside
    them never count. Spans: (start, end, kind) with kind s (literal) · c (comment) · x (JSX text) · h (not PHP)."""

    def __init__(self, s, kind, jsx=False):
        self.s, self.n, self.kind, self.spans = s, len(s), kind, []
        self.jsx = jsx
        self.rx = _LEX_RX["jsx" if jsx else kind]

    def run(self):
        i = 0
        if self.kind == "php":
            j = self.s.find("<?php")
            j = self.s.find("<?") if j < 0 else j
            if j < 0:
                self.spans.append((0, self.n, "h"))
                return self
            if j > 0:
                self.spans.append((0, j, "h"))
            i = j + 2
        elif self.s.startswith("#!"):
            i = self._line(0)
        self.code(i, False)
        return self

    def code(self, i, stop):
        s, n, rx, kind = self.s, self.n, self.rx, self.kind
        depth = 0
        while i < n:
            m = rx.search(s, i)
            if not m:
                return n
            j = m.start()
            c = s[j]
            if c == "{":
                depth += 1
                i = j + 1
            elif c == "}":
                if stop and depth == 0:
                    return j
                depth = max(0, depth - 1)
                i = j + 1
            elif c == "/":
                nx = s[j + 1: j + 2]
                if nx == "/":
                    i = self._line(j)
                elif nx == "*":
                    i = self._block(j)
                elif kind == "js" and self._regex_ok(j):
                    i = self._regex(j) or j + 1
                else:
                    i = j + 1
            elif c == "#":
                i = j + 2 if s.startswith("#[", j) else self._line(j)
            elif c == "<":
                if kind == "php":
                    i = self._heredoc(j) if s.startswith("<<<", j) else j + 1
                else:
                    i = (self._jsx(j) if self._jsx_ok(j) else None) or j + 1
            elif c == "`":
                if kind == "js":
                    i = self._template(j)
                else:
                    i = self._quoted(j, "`", multiline=kind == "go", escapes=False)
            elif c == '"' and kind in ("java", "kotlin") and s.startswith('"""', j):
                i = self._triple(j)
            elif c == '"' and kind == "kotlin":
                i = self._kstring(j)
            else:
                i = self._quoted(j, c, multiline=kind == "php", escapes=True)
        return n

    def _quoted(self, j, q, multiline, escapes):
        key = (q, multiline, escapes)
        pat = _QCACHE.get(key)
        if pat is None:
            pat = _QCACHE[key] = re.compile("[" + re.escape(q) + ("\\\\" if escapes else "") +
                                            ("" if multiline else "\n") + "]")
        s, n, k = self.s, self.n, j + 1
        while True:
            m = pat.search(s, k)
            if not m:
                e = n
                break
            p, c = m.start(), m.group()
            if c == "\\":
                k = p + 2
                continue
            e = p if c == "\n" else p + 1
            break
        self.spans.append((j, e, "s"))
        return e

    def _template(self, j):
        s, n, k = self.s, self.n, j + 1
        while True:
            m = _TPL.search(s, k)
            if not m:
                e = n
                break
            g = m.group()
            if g == "\\":
                k = m.start() + 2
            elif g == "`":
                e = m.start() + 1
                break
            else:
                k = self.code(m.end(), True) + 1
        self.spans.append((j, e, "s"))
        return e

    def _kstring(self, j):
        s, n, k = self.s, self.n, j + 1
        while True:
            m = _KSTR.search(s, k)
            if not m:
                e = n
                break
            g = m.group()
            if g == "\\":
                k = m.start() + 2
            elif g == '"':
                e = m.start() + 1
                break
            elif g == "\n":
                e = m.start()
                break
            else:
                k = self.code(m.end(), True) + 1
        self.spans.append((j, e, "s"))
        return e

    def _triple(self, j):
        s, n, k = self.s, self.n, j + 3
        pat = _KRAW if self.kind == "kotlin" else _JTB
        while True:
            m = pat.search(s, k)
            if not m:
                e = n
                break
            g = m.group()
            if g == "\\":
                k = m.start() + 2
            elif g == '"""':
                e = m.end()
                while e < n and s[e] == '"':
                    e += 1
                break
            else:
                k = self.code(m.end(), True) + 1
        self.spans.append((j, e, "s"))
        return e

    def _line(self, j):
        e = self.s.find("\n", j)
        e = self.n if e < 0 else e
        self.spans.append((j, e, "c"))
        return e

    def _block(self, j):
        s, n = self.s, self.n
        if self.kind == "kotlin":
            depth, k = 0, j
            while k < n:
                a, b = s.find("/*", k), s.find("*/", k)
                if b < 0:
                    k = n
                    break
                if 0 <= a < b:
                    depth += 1
                    k = a + 2
                else:
                    depth -= 1
                    k = b + 2
                    if depth == 0:
                        break
            e = min(k, n)
        else:
            e = s.find("*/", j + 2)
            e = n if e < 0 else e + 2
        self.spans.append((j, e, "c"))
        return e

    def _prev(self, j):
        k = j - 1
        s = self.s
        while k >= 0 and s[k] in " \t\r\n":
            k -= 1
        return k

    def _regex_ok(self, j):
        s = self.s
        k = self._prev(j)
        if k < 0:
            return True
        c = s[k]
        if c in "(,=:[!&|?{};+-*%<>~^":
            return True
        if c.isalnum() or c in "_$":
            e = k + 1
            while k >= 0 and (s[k].isalnum() or s[k] in "_$"):
                k -= 1
            return s[k + 1:e] in _REGEX_KW
        return False

    def _regex(self, j):
        s, n, k, cls = self.s, self.n, j + 1, False
        while k < n:
            c = s[k]
            if c == "\\":
                k += 2
                continue
            if c == "\n":
                return None
            if cls:
                cls = c != "]"
            elif c == "[":
                cls = True
            elif c == "/":
                break
            k += 1
        else:
            return None
        k += 1
        while k < n and s[k].isalpha():
            k += 1
        self.spans.append((j, k, "s"))
        return k

    def _jsx_ok(self, j):
        s = self.s
        nx = s[j + 1: j + 2]
        if not (nx.isalpha() or nx == ">"):
            return False
        k = self._prev(j)
        if k < 0:
            return True
        c = s[k]
        if c in "(,=:[!&|?{};":
            return True
        if c == ">" and k > 0 and s[k - 1] == "=":
            return True
        if c.isalpha():
            e = k + 1
            while k >= 0 and s[k].isalpha():
                k -= 1
            return s[k + 1:e] in ("return", "yield", "default", "case", "await")
        return False

    def _jsx(self, j):
        mark = len(self.spans)
        try:
            r = self._jsx_el(j, 0)
        except RecursionError:
            r = None
        if r is None:
            del self.spans[mark:]
        return r

    def _jsx_el(self, j, depth):
        s, n = self.s, self.n
        if depth > 60:
            return None
        k = j + 1
        m = _JSX_NAME.match(s, k)
        k = m.end() if m else k
        while True:
            k = _ws(s, k)
            if k >= n:
                return None
            if s.startswith("/>", k):
                return k + 2
            c = s[k]
            if c == ">":
                k += 1
                break
            if c == "{":
                e = self.code(k + 1, True)
                if e >= n:
                    return None
                k = e + 1
                continue
            am = _JSX_ATTR.match(s, k)
            if not am:
                return None
            k = _ws(s, am.end())
            if k < n and s[k] == "=":
                k = _ws(s, k + 1)
                if k >= n:
                    return None
                c = s[k]
                if c in "\"'":
                    e = s.find(c, k + 1)
                    if e < 0:
                        return None
                    self.spans.append((k, e + 1, "s"))
                    k = e + 1
                elif c == "{":
                    e = self.code(k + 1, True)
                    if e >= n:
                        return None
                    k = e + 1
                elif c == "<":
                    r = self._jsx_el(k, depth + 1)
                    if r is None:
                        return None
                    k = r
                else:
                    return None
        while True:
            m = _JSX_CHILD.search(s, k)
            if not m:
                return None
            p = m.start()
            if p > k:
                self.spans.append((k, p, "x"))
            if s[p] == "{":
                e = self.code(p + 1, True)
                if e >= n:
                    return None
                k = e + 1
                continue
            if s.startswith("</", p):
                e = s.find(">", p)
                return None if e < 0 else e + 1
            r = self._jsx_el(p, depth + 1)
            if r is None:
                return None
            k = r

    def _heredoc(self, j):
        m = _HEREDOC.match(self.s, j)
        if not m:
            return j + 3
        end = re.compile(r"^[ \t]*" + re.escape(m.group(2)) + r"\b", re.M).search(self.s, m.end())
        e = end.end() if end else self.n
        self.spans.append((j, e, "s"))
        return e


class _Src:
    """A source file with its masked twin: same length, literals and comments blanked, so regexes and bracket
    matching see only code. `nc` keeps literals and blanks only comments (for comparing bodies)."""

    def __init__(self, text, kind, jsx=False):
        self.text = text
        lx = _Lexer(text, kind, jsx).run()
        spans = sorted(lx.spans, key=lambda x: (x[0], -x[1]))
        mp, cp, last = [], [], 0
        self.comment_lines, self.comments = set(), []
        self.nl = [m.start() for m in re.finditer("\n", text)]
        for a, b, k in spans:
            if a < last:
                continue
            mp.append(text[last:a])
            cp.append(text[last:a])
            seg = text[a:b]
            if k == "s" and b - a >= 2:
                mp.append(seg[0] + _NOT_NL.sub(" ", seg[1:-1]) + seg[-1])
                cp.append(seg)
            elif k == "x":
                mp.append(_NOT_NL.sub(" ", seg))
                cp.append(seg)
            else:
                blank = _NOT_NL.sub(" ", seg)
                mp.append(blank)
                cp.append(blank if k in "ch" else seg)
                if k == "c":
                    self.comment_lines.update(range(self.line(a), self.line(max(a, b - 1)) + 1))
                    self.comments.append((a, b))
            last = b
        mp.append(text[last:])
        cp.append(text[last:])
        self.m, self.nc = "".join(mp), "".join(cp)
        self.pairs = {}
        stack = []
        for mm in _BRK.finditer(self.m):
            if mm.group() in "({[":
                stack.append(mm.start())
            elif stack:
                self.pairs[stack.pop()] = mm.start()

    def line(self, pos):
        return bisect.bisect_left(self.nl, pos) + 1

    def line_start(self, n):
        return 0 if n <= 1 else self.nl[min(n, len(self.nl) + 1) - 2] + 1

    def comments_in(self, a, b) -> str:
        return " ".join(self.text[x:y] for x, y in self.comments if x < b and y > a)

    def doc(self, test_line, bs, be, above=2) -> str:
        """Comments inside a test body and on the lines just above it."""
        return self.comments_in(self.line_start(max(1, test_line - above)), be)

    def call_text(self, pos) -> str:
        """The assertion call that starts at pos, for messages."""
        k = self.m.find("(", pos, pos + 60)
        if k >= 0 and k in self.pairs:
            end = self.pairs[k] + 1
            sep = "->" if re.match(r"\s*\??->", self.m[end:end + 20]) else "."
            for _, args, npos in self.chain(end, sep):
                end = args[1] + 1 if args else _IDENT.match(self.m, npos).end()
            return self.text[pos:end]
        e = self.m.find("\n", pos)
        return self.text[pos:e if e > 0 else pos + 80]

    def norm(self, a, b):
        return " ".join(self.nc[a:b].split()).strip(" ;")

    def args(self, o, c):
        """Top-level argument spans of the bracket group at o..c."""
        M, out, start, i = self.m, [], o + 1, o + 1
        while i < c:
            ch = M[i]
            if ch in "([{":
                i = self.pairs.get(i, i) + 1
                continue
            if ch == ",":
                out.append((start, i))
                start = i + 1
            i += 1
        if M[start:c].strip():
            out.append((start, c))
        return out

    def chain(self, j, sep="."):
        """[(name, args span or None, name pos)] for .a(...).b.c(...) (or ->) starting at j."""
        M, out = self.m, []
        sep_re = _CHAIN_ARROW if sep != "." else _CHAIN_DOT
        while True:
            m = sep_re.match(M, j)
            if not m:
                break
            mm = _IDENT.match(M, m.end())
            if not mm:
                break
            k = _ws(M, mm.end())
            if k < len(M) and M[k] == "(" and k in self.pairs:
                out.append((mm.group(), (k + 1, self.pairs[k]), mm.start()))
                j = self.pairs[k] + 1
            else:
                out.append((mm.group(), None, mm.start()))
                j = mm.end()
        return out

    def enclosing(self, a, pos):
        """Opening braces between a and pos whose block contains pos (outermost first)."""
        out = []
        for m in re.finditer(r"\{", self.m[a:pos]):
            o = a + m.start()
            if self.pairs.get(o, -1) > pos:
                out.append(o)
        return out

    def enclosing_any(self, a, pos):
        """Opening brackets of any kind between a and pos whose group contains pos."""
        return [a + m.start() for m in re.finditer(r"[({\[]", self.m[a:pos]) if self.pairs.get(a + m.start(), -1) > pos]

    def in_loop(self, a, pos, lang):
        M = self.m
        ls = M.rfind("\n", 0, pos) + 1
        if re.match(r"\s*(?:\}\s*)?(?:for|while|foreach|do|repeat)\b", M[ls:pos]):
            return True
        for o in self.enclosing(a, pos):
            head = M[max(a, o - 300):o]
            if lang == "go":
                if re.search(r"\bfor\b[^{}]*$", head):
                    return True
            elif re.search(r"(?:\b(?:for|while|foreach)\s*\([^{};]*(?:;[^{};]*){0,2}\)|\bdo|\brepeat\s*\([^{}]*\)|"
                           r"\bloop)\s*$", head):
                return True
        return False

    def top_level(self, a, pos):
        return not self.enclosing(a, pos)


_CHAIN_DOT = re.compile(r"\s*\??\.\s*")
_CHAIN_ARROW = re.compile(r"\s*\??->\s*")
_JSX_NAME = re.compile(r"[A-Za-z_$][\w$.:-]*")
_JSX_ATTR = re.compile(r"[A-Za-z_$][\w$:.-]*")
_JSX_CHILD = re.compile(r"[<{]")
_LIT = re.compile(r"""^(?:true|false|null|undefined|nil|None|TRUE|FALSE|NULL|True|False|-?\d[\d_]*(?:\.\d+)?[lLfFdD]?|
                  '(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n$]|\\.)*"|`[^`$\\]*`)$""", re.X)


def _lit(text):
    """(True, value) for a literal, else (False, None)."""
    t = text.strip()
    if not _LIT.match(t):
        return False, None
    low = t.lower()
    if low in ("true", "false"):
        return True, low == "true"
    if low in ("null", "undefined", "nil", "none"):
        return True, None
    if t[0] in "'\"`":
        return True, t[1:-1]
    try:
        return True, float(t.rstrip("lLfFdD").replace("_", ""))
    except ValueError:
        return False, None


def _same_text(a, b):
    a, b = " ".join(a.split()), " ".join(b.split())
    return bool(a) and a == b and "(" not in a and not _lit(a)[0]


def _lit_or_same(a, b):
    oka, va = _lit(a)
    okb, vb = _lit(b)
    if oka and okb:
        return va == vb
    return _same_text(a, b)


def _roots(text, drop=()):
    """Root identifiers of an expression in masked code (not member names, keywords or well-known globals)."""
    out = set()
    for m in re.finditer(r"(?<![\w$.>:\\])(\$?[A-Za-z_][\w$]*)", text):
        w = m.group(1)
        if w in _KEYWORDS or w in drop or (w[0].isupper() and not w.isupper()):
            continue
        out.add(w.lstrip("$"))
    return out


_KEYWORDS = {"true", "false", "null", "undefined", "nil", "new", "this", "self", "typeof", "await", "async", "void",
             "instanceof", "in", "of", "is", "as", "class", "return", "function", "fn", "let", "const", "var", "val",
             "super", "static", "len", "cap", "string", "int", "float64", "byte", "rune", "error", "make", "JSON",
             "Math", "Object", "Array", "String", "Number", "Boolean", "Promise", "it", "and", "or", "not", "if",
             "else", "array", "list", "isset", "count", "empty", "sizeof", "strlen", "any", "eq"}


@dataclass
class _Occ:
    pos: int
    kind: str
    text: str = ""


def _fill(src: _Src, t: _T, occs: list, a: int, b: int, lang: str):
    """Record each assertion's kind, and the first trivial one for the message."""
    for o in occs:
        t.kinds.append(o.kind)
        if o.kind == "trivial" and not t.trivial:
            t.trivial = _clip(o.text)


def _swallowed(src: _Src, t: _T, occs: list, a: int, b: int, try_re, catch_re, broad, fn_ranges, quiet_re,
               fam: str):
    M = src.m
    for m in try_re.finditer(M, a, b):
        to = m.end() - 1
        tc = src.pairs.get(to)
        if tc is None or tc > b:
            continue
        inner = [o for o in occs if to < o.pos < tc and not any(x < o.pos < y for x, y in fn_ranges)]
        if not inner:
            continue
        k = tc + 1
        while True:
            cm = catch_re.match(M, k)
            if not cm:
                break
            ho = cm.end() - 1
            hc = src.pairs.get(ho)
            if hc is None:
                break
            types = cm.group(1) or ""
            desc = broad(types)
            if desc and _quiet(src, ho + 1, hc, quiet_re):
                t.swallowed.append((src.line(inner[0].pos), desc))
                return
            k = hc + 1


def _quiet(src: _Src, a, b, quiet_re) -> bool:
    """Is a handler body only prints, logs, returns and empty statements?"""
    M = src.m
    rest, i = [], a
    for m in quiet_re.finditer(M, a, b):
        if m.start() < i:
            continue
        rest.append(M[i:m.start()])
        j = m.end()
        if j - 1 < len(M) and M[j - 1] == "(":
            j = src.pairs.get(j - 1, j - 1) + 1
        i = j
    rest.append(M[i:b])
    left = re.sub(r"\b(?:return|continue|break|Unit|null)\b|[;\s]", "", "".join(rest))
    return left == ""


def _helpers(src: _Src, decl_re, body_of, assert_re, max_rounds=6):
    """Names of functions defined in the file whose body asserts (directly or through another such function)."""
    bodies = {}
    for m in decl_re.finditer(src.m):
        r = body_of(m)
        if r:
            bodies.setdefault(m.group(1), r)
    found = {n for n, (a, b) in bodies.items() if assert_re.search(src.m, a, b)}
    for _ in range(max_rounds):
        if not found:
            break
        call = re.compile(r"(?<![\w$])(?:" + "|".join(re.escape(x) for x in sorted(found)) + r")\s*\(")
        more = {n for n, (a, b) in bodies.items() if n not in found and call.search(src.m, a, b)}
        if not more:
            break
        found |= more
    return found, bodies


def _calls_any(src, names, a, b, prefix=r"(?<![\w$.])"):
    if not names:
        return None
    rx = re.compile(prefix + r"(?:" + "|".join(re.escape(x) for x in sorted(names)) + r")\s*\(")
    return rx.search(src.m, a, b)


def _subject(src, a, b, skip, rx=re.compile(r"(?<![\w$])([A-Za-z_][\w$]*)\s*\(")):
    last = ""
    for m in rx.finditer(src.m, a, b):
        w = m.group(1)
        if w in skip or w in _KEYWORDS or re.match(r"(?i)^(?:assert|expect|verify|check|should|mock|spy|stub)", w):
            continue
        last = w
    return last


# --------------------------------------------------------------------------- JavaScript / TypeScript

_JS_CALLEE = re.compile(r"(?<![\w$.])(it|test|specify|xit|xtest|fit|xspecify|describe|context|suite|fdescribe|"
                        r"xdescribe|xcontext)\b")
_JS_TESTS = {"it", "test", "specify", "xit", "xtest", "fit", "xspecify"}
_JS_MODS = {"only", "skip", "todo", "concurrent", "sequential", "each", "failing", "fails", "skipIf", "runIf",
            "fixme", "serial", "parallel"}
_JS_FN_KW = re.compile(r"(?:async\s+)?function\b\s*\*?\s*[\w$]*\s*(?=\()")
_JS_ASSERT = re.compile(r"(?<![\w$])_*(expect\w*)\s*(?=[(<.])|(?<![\w$])_*(assert\w*)\s*(?=[(.<])|"
                        r"(?<![\w$])_*((?:check|verify|should)\w*)\s*(?=[(<])|\.\s*(should)\b|"
                        r"(?<![\w$.])(fail)\s*\(|(?<![\w$.])(throw)\b")
_JS_SNAP = {"toMatchSnapshot", "toMatchFileSnapshot", "toThrowErrorMatchingSnapshot"}
_JS_MOCK_M = re.compile(r"^(?:toHaveBeenCalled\w*|toBeCalled\w*|toHaveBeenNthCalledWith|toHaveBeenLastCalledWith|"
                        r"lastCalledWith|nthCalledWith|toHaveReturned\w*|toReturn\w*|called\w*|toHaveBeenCalledTimes)$")
_JS_MOCK_DECL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*(?::[^=;\n]+)?=\s*(?:(?:jest|vi|sinon|sandbox|jasmine)"
                           r"\s*\.\s*(?:fn|spyOn|stub|spy|fake|mock|createSpy|createSpyObj|mocked)\b|"
                           r"(?:createSpy|mock)\s*[<(])")
_JS_DERIVED = re.compile(r"(?<![\w$.])(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=;\n]+)?=\s*(?:await\s+)?"
                         r"([A-Za-z_$][\w$]*)\b")
_JS_IMPORT = re.compile(r"(?<![\w$.])import\s*(?:type\s+)?([^;'\"`()]*?)\s*(?:from\s*)?(['\"])")
_JS_REQUIRE = re.compile(r"(?<![\w$.])(?:(?:const|let|var)\s+([^=;]+?)\s*=\s*)?require\s*\(\s*(['\"])")
_JS_DYN = re.compile(r"(?<![\w$.])import\s*\(\s*(['\"]?)|(?:jest|vi)\s*\.\s*(?:mock|doMock|unmock)\s*\(\s*(['\"])")
_JS_FN_DECL = re.compile(r"(?<![\w$.])(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(")
_JS_FN_VAR = re.compile(r"(?<![\w$.])(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=;\n]+)?=\s*"
                        r"(?=(?:async\s*)?(?:function\b|\([^()]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>))")
_JS_SLEEP = re.compile(r"new\s+Promise\s*\(|(?<![\w$.])(sleep|delay|wait|pause)\s*\(\s*(?!0\s*[,)])[\w$.]|"
                       r"\.\s*(waitForTimeout)\s*\(|(?<![\w$.])cy\s*\.\s*(wait)\s*\(\s*\d|"
                       r"(?<![\w$.])browser\s*\.\s*(pause)\s*\(|(?<![\w$])await\s+(setTimeout)\s*\(")
_JS_FAKE = re.compile(r"useFakeTimers|advanceTimers|runAllTimers|runOnlyPendingTimers|FakeTimers|\bclock\s*\.\s*tick")
_JS_PRINT = re.compile(r"(?<![\w$.])console\s*\.\s*(log|debug|info|dir|trace|table)\s*\(")
_JS_E2E = re.compile(r"(?<![\w$.])(?:cy|page|browser|driver)\s*\.\s*[\w$]+\s*[(.]")
_JS_PARAM_QUIET = {"test", "skip", "todo", "diagnostic", "plan", "comment", "log", "timeout", "teardown", "before",
                   "after", "beforeEach", "afterEach", "mock", "name", "signal", "runOnly", "end", "context", "step",
                   "slow", "fixme", "info", "setTimeout", "annotations", "attach"}
_JS_BUILTINS = {"assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants", "crypto",
                "dgram", "diagnostics_channel", "dns", "domain", "events", "fs", "http", "http2", "https", "inspector",
                "module", "net", "os", "path", "perf_hooks", "process", "punycode", "querystring", "readline", "repl",
                "stream", "string_decoder", "sys", "timers", "tls", "trace_events", "tty", "url", "util", "v8", "vm",
                "wasi", "worker_threads", "zlib", "test", "sqlite"}
_JS_FRAMEWORKS = re.compile(r"^(?:vitest|@vitest/.*|jest|@jest/.*|jest-.*|mocha|chai|chai-.*|sinon|sinon-.*|ava|tap|"
                            r"uvu|uvu/.*|expect|should|power-assert|fast-check|@fast-check/.*|node:test)$")
_JS_UNSURE = re.compile(r"(?<![\w$.])(?:child_process|execa|spawn\w*|exec\w*|fork|readFile\w*|readdir\w*|fetch|"
                        r"__dirname|__filename|window|document|globalThis|global|request|axios|cy|page|browser)\b|"
                        r"import\s*\.\s*meta|process\s*\.\s*cwd|(?<![\w$.])fs\s*\.")
_JS_NOT_SUBJECT = {"expect", "it", "test", "describe", "require", "console", "setTimeout", "Promise", "render",
                   "screen", "within", "fireEvent", "userEvent", "act", "waitFor", "cleanup", "fn", "spyOn",
                   "mockReturnValue", "mockResolvedValue", "mockImplementation", "toBe", "toEqual", "map", "filter",
                   "forEach", "push", "then", "catch", "resolve", "reject", "join", "toString", "keys", "values",
                   "entries", "stringify", "from", "of", "includes", "some", "every",
                   "slice", "split", "trim", "useFakeTimers", "advanceTimersByTime", "clearAllMocks", "beforeEach",
                   "afterEach", "toJSON", "length", "getByText", "getByRole", "queryByText", "findByText"}


class _JsFile:
    def __init__(self, text, lang, rel):
        self.lang = lang
        self.src = _Src(text, "js", jsx=lang in ("jsx", "js"))
        src, M = self.src, self.src.m
        self.rel = rel
        self.specs, self.support_names, mocked_specs = [], set(), set()
        for m in _JS_DYN.finditer(M):
            q = m.group(1) if m.group(1) is not None else m.group(2)
            if m.group(2):
                mocked_specs.add(self._spec(m.start(2)))
            elif q:
                self.specs.append((self._spec(m.start(1)), ""))
            else:
                self.specs.append(("<dynamic>", ""))
        for m in _JS_IMPORT.finditer(M):
            self.specs.append((self._spec(m.start(2)), m.group(1)))
        for m in _JS_REQUIRE.finditer(M):
            self.specs.append((self._spec(m.start(2)), m.group(1) or ""))
        self.mocks = {m.group(1) for m in _JS_MOCK_DECL.finditer(M)}
        for spec, binds in self.specs:
            names = set(_IDENT.findall(re.sub(r"\b\w+\s+as\b|\btype\b|\*\s*as\b|\w+\s*:", " ", binds)))
            if _SUPPORT.search(spec) and (spec.startswith((".", "@/")) or "/" in spec):
                self.support_names |= names
            if spec in mocked_specs:
                self.mocks |= names
        for _ in range(2):
            self.mocks |= {m.group(1) for m in _JS_DERIVED.finditer(M) if m.group(2) in self.mocks}
        self.fake_timers = bool(_JS_FAKE.search(M))
        self.helpers, _ = _helpers(src, _JS_FN_DECL, self._decl_body, _JS_ASSERT)
        h2, _ = _helpers(src, _JS_FN_VAR, self._var_body, _JS_ASSERT)
        self.helpers |= h2

    def _spec(self, q):
        T, M = self.src.text, self.src.m
        e = M.find(T[q], q + 1)
        return T[q + 1:e] if e > q else ""

    def _decl_body(self, m):
        po = m.end() - 1
        pc = self.src.pairs.get(po)
        if pc is None:
            return None
        bo = self.src.m.find("{", pc)
        bc = self.src.pairs.get(bo) if bo >= 0 else None
        return (bo + 1, bc) if bc else None

    def _var_body(self, m):
        r = _js_fn(self.src, m.end(), len(self.src.m))
        if not r:
            return None
        _, a, b, block = r
        if not block:
            nl = self.src.m.find("\n", a)
            b = nl if nl > 0 else b
        return a, b


def _js_fn(src: _Src, a, b):
    """(params text, body start, body end, block?) for a function literal at src.m[a:b], else None."""
    M, pairs = src.m, src.pairs
    i = _ws(M, a)
    m = _JS_FN_KW.match(M, i)
    if m:
        po = m.end()
        pc = pairs.get(po)
        if pc is None or pc >= b:
            return None
        bo = M.find("{", pc, b)
        bc = pairs.get(bo) if bo >= 0 else None
        if bc is None or bc > b:
            return None
        return M[po + 1:pc], bo + 1, bc, True
    if M.startswith("async", i) and not M[i + 5:i + 6].isalnum():
        i = _ws(M, i + 5)
    if i < b and M[i] == "(":
        pc = pairs.get(i)
        if pc is None or pc >= b:
            return None
        j = M.find("=>", pc, b)
        between = M[pc + 1:j].strip() if j >= 0 else "x"
        if j < 0 or (between and not between.startswith(":")):
            return None
        params = M[i + 1:pc]
    else:
        m = _IDENT.match(M, i)
        if not m:
            return None
        j = _ws(M, m.end())
        if not M.startswith("=>", j):
            return None
        params = m.group()
    k = _ws(M, j + 2)
    if k < b and M[k] == "{":
        bc = pairs.get(k)
        if bc is None or bc > b:
            return None
        return params, k + 1, bc, True
    return params, k, b, False


def _js_call(src: _Src, start):
    """(mods, groups, (open, close)) for a callee chain starting at `start`, else None."""
    M, pairs, n = src.m, src.pairs, len(src.m)
    i, mods, groups = start, [], []
    while True:
        i = _ws(M, i)
        c = M[i] if i < n else ""
        if c == ".":
            mm = _IDENT.match(M, _ws(M, i + 1))
            if not mm:
                return None
            mods.append(mm.group())
            i = mm.end()
            continue
        if c in ("(", "`"):
            close = pairs.get(i) if c == "(" else M.find("`", i + 1)
            if close is None or close < 0:
                return None
            j = _ws(M, close + 1)
            if j < n and M[j] in "(`":
                groups.append((i, close))
                i = j
                continue
            if c == "`":
                return None
            if j < n and M[j] == "{":
                return None  # a method definition: test(x) { ... }
            return mods, groups, (i, close)
        return None


def _js_name(src: _Src, a, b):
    t = src.text[a:b].strip()
    if len(t) >= 2 and t[0] in "'\"`" and t[-1] == t[0]:
        return t[1:-1]
    return t


def _js_occs(jf: _JsFile, a, b, params=()):
    src, M = jf.src, jf.src.m
    out = []
    for m in _JS_ASSERT.finditer(M, a, b):
        pos = m.start()
        if m.group(1):
            out.append(_Occ(pos, _js_expect(jf, m), src.call_text(pos)))
        elif m.group(2):
            out.append(_Occ(pos, _js_assert(jf, m), src.call_text(pos)))
        else:
            out.append(_Occ(pos, "real"))
    for p in params:
        hand = re.search(r"[(,]\s*" + re.escape(p) + r"\s*[,)]", M[a:b])
        if hand:  # runTest(t, cases): the helper asserts through the test object
            out.append(_Occ(a + hand.start(), "real"))
        for m in re.finditer(r"(?<![\w$.])" + re.escape(p) + r"\s*(?:\.\s*([\w$]+)\s*)?\(", M[a:b]):
            if m.group(1) in _JS_PARAM_QUIET:
                continue
            if m.group(1) is None:
                k = a + m.end()
                if not M[k:src.pairs.get(k - 1, k)].strip():
                    continue  # done() without an error argument
            out.append(_Occ(a + m.start(), "real"))
    for names in (jf.helpers, jf.support_names):
        for m in re.finditer(r"(?<![\w$.])(?:" + "|".join(re.escape(x) for x in sorted(names)) + r")\s*\(",
                             M[a:b]) if names else ():
            out.append(_Occ(a + m.start(), "real"))
    for m in re.finditer(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\.\s*[\w$]+\s*\(", M[a:b]):
        if m.group(1) in jf.support_names:
            out.append(_Occ(a + m.start(), "real"))
    out.sort(key=lambda o: o.pos)
    return out


def _js_expect(jf: _JsFile, m):
    src, M = jf.src, jf.src.m
    k = _ws(M, m.end())
    if k >= len(M) or M[k] != "(" or k not in src.pairs:
        return "real"  # expect.assertions(n), expect.poll, expect<T>
    sc = src.pairs[k]
    subj = src.text[k + 1:sc]
    subj_m = M[k + 1:sc]
    chain = src.chain(sc + 1)
    names = [c[0] for c in chain]
    if any(n in _JS_SNAP for n in names):
        return "snapshot"
    if any(_JS_MOCK_M.match(n) for n in names):
        return "mock"
    if _js_trivial(src, subj, chain):
        return "trivial"
    r = _roots(subj_m)
    if r and r <= jf.mocks:
        return "mock"
    return "real"


def _js_trivial(src, subj, chain) -> bool:
    if not chain:
        return False
    neg = sum(1 for c in chain if c[0] == "not") % 2 == 1
    name, args, _ = chain[-1]
    ok, v = _lit(subj)
    if name in ("toBe", "toEqual", "toStrictEqual", "equal", "equals", "eql", "deepEqual", "strictEqual") and args:
        a = src.text[args[0]:args[1]]
        oka, va = _lit(a)
        if ok and oka:
            return (v == va) != neg
        return _same_text(subj, a) and not neg
    if not ok or args is not None and name not in ("toBeTruthy", "toBeFalsy", "toBeNull", "toBeUndefined",
                                                   "toBeDefined", "toBeTrue", "toBeFalse"):
        return False
    truth = {"toBeTruthy": bool(v), "toBeFalsy": not v, "toBeNull": v is None, "toBeUndefined": subj.strip() ==
             "undefined", "toBeDefined": subj.strip() != "undefined", "toBeTrue": v is True, "toBeFalse": v is False,
             "true": v is True, "false": v is False, "ok": bool(v), "null": v is None,
             "undefined": subj.strip() == "undefined"}.get(name)
    return truth is not None and truth != neg


def _js_assert(jf: _JsFile, m):
    src, M = jf.src, jf.src.m
    start = m.start()
    pre = M[max(0, start - 8):start]
    if re.search(r"sinon\s*\.\s*$", pre):
        return "mock"
    j = m.end()
    chain = src.chain(j)
    method, args = ("ok", None)
    k = _ws(M, j)
    if chain and m.group(2) == "assert":
        method, args, _ = chain[0]
    elif k < len(M) and M[k] == "(" and k in src.pairs:
        method, args = (m.group(2) if m.group(2) != "assert" else "ok"), (k + 1, src.pairs[k])
    if args is None:
        return "real"
    if method.startswith("called") or method in ("notCalled", "alwaysCalledWith"):
        return "mock"
    if method == "snapshot":
        return "snapshot"
    parts = [src.text[x:y] for x, y in src.args(args[0] - 1, args[1])]
    if method in ("ok", "isTrue", "isOk", "assert") and parts:
        ok, v = _lit(parts[0])
        return "trivial" if ok and bool(v) else "real"
    if method in ("equal", "strictEqual", "deepEqual", "deepStrictEqual", "equals") and len(parts) >= 2:
        return "trivial" if _lit_or_same(parts[0], parts[1]) else "real"
    if method in ("notEqual", "notStrictEqual") and len(parts) >= 2:
        oka, va = _lit(parts[0])
        okb, vb = _lit(parts[1])
        return "trivial" if oka and okb and va != vb else "real"
    return "real"


def _js_fn_ranges(src: _Src, a, b):
    """Bodies of the functions defined inside a test body (callbacks), as (start, end) ranges."""
    M, out = src.m, []
    for m in re.finditer(r"=>\s*\{|=>|(?<![\w$.])function\b[^{(]*\(", M[a:b]):
        p = a + m.end() - 1
        if M[p] == ">":  # an expression body: up to the end of the enclosing bracket group
            close = min((src.pairs[o] for o in src.enclosing_any(a, p) if src.pairs[o] < b), default=b)
            out.append((p, close))
        elif M[p] == "{":
            out.append((p, src.pairs.get(p, p)))
        else:
            pc = src.pairs.get(p)
            bo = M.find("{", pc) if pc else -1
            if bo >= 0:
                out.append((bo, src.pairs.get(bo, bo)))
    return out


_JS_TRY = re.compile(r"(?<![\w$.])try\s*\{")
_JS_CATCH = re.compile(r"\s*catch\s*(?:\(([^)]*)\))?\s*\{")
_JS_QUIET = re.compile(r"(?<![\w$.])console\s*\.\s*\w+\s*\(")


def _analyze_js(text, path, base, rel, lang):
    jf = _JsFile(text, lang, rel)
    src, M = jf.src, jf.src.m
    suites, tests, annotations = [], [], []
    for m in _JS_CALLEE.finditer(M):
        if re.search(r"(?:function|class|new|interface|type)\s*$", M[max(0, m.start() - 12):m.start()]):
            continue
        r = _js_call(src, m.end())
        if not r:
            continue
        mods, groups, (o, c) = r
        if any(x not in _JS_MODS for x in mods):
            continue
        callee = m.group(1)
        args = src.args(o, c)
        fn = None
        for x, y in args[1:]:
            fn = _js_fn(src, x, y)
            if fn:
                break
        if not args:
            if mods and mods[-1] in ("skip", "fixme"):
                annotations.append((m.start(), "noreason"))
            continue
        name = _js_name(src, *args[0])
        line = src.line(m.start())
        skipped = callee.startswith("x") or "skip" in mods or "fixme" in mods
        reasoned = bool({line, line - 1} & src.comment_lines)
        if callee in _JS_TESTS or callee == "fit":
            if "todo" in mods:
                continue
            if fn is None and mods and mods[-1] in ("skip", "fixme"):
                # Playwright test.skip(condition, 'reason') inside a test or a describe block
                kind = "reason" if any(src.m[x:y].strip()[:1] in "'\"`" for x, y in args) else "cond"
                annotations.append((m.start(), kind))
                continue
            t = _T(name=name, line=line, bare=name, skipped=skipped)
            if skipped and not reasoned:
                t.skip_line = line
            if fn is None:
                t.body = False  # a function defined elsewhere, or a pending test
                tests.append((t, None))
                continue
            params, bs, be, block = fn
            t.dup = "|".join(src.norm(x + 1, y) for x, y in groups) + "#" + src.norm(bs, be)
            tests.append((t, (params, bs, be, block, m.start())))
        else:
            if fn is None:
                continue
            suites.append({"name": name, "line": line, "a": fn[1], "b": fn[2], "skipped": skipped,
                           "noreason": skipped and not reasoned})
    extra = [_finding(rel, s["line"], s["name"], "skipped-without-reason", "is skipped with no reason", _FIX_SKIP["js"])
             for s in suites if s["noreason"]]
    out, bodies = [], {}
    for t, info in tests:
        pos = info[4] if info else None
        if pos is None:
            out.append(t)
            continue
        inside = [s for s in suites if s["a"] <= pos < s["b"]]
        if inside:
            t.scope = str(max(s["a"] for s in inside))
            t.skipped = t.skipped or any(s["skipped"] for s in inside)
        params, bs, be, block, _ = info
        for apos, kind in annotations:
            if bs <= apos < be:
                if kind == "cond":
                    t.implicit = True
                else:
                    t.skipped = True
                    if kind == "noreason":
                        t.skip_line = src.line(apos)
        _js_test(jf, t, params, bs, be, block)
        bodies[id(t)] = src.m[bs:be]
        out.append(t)
    if out and _js_no_prod(jf):
        extra.append(_no_prod(rel, out[0].line))
    return out, extra, bodies


def _js_test(jf: _JsFile, t: _T, params, bs, be, block):
    src, M = jf.src, jf.src.m
    if not M[bs:be].strip() or (not block and M[bs:be].strip() in ("undefined", "null", "{}")):
        t.empty = True
        return
    if _smoke(t.name, t.name + " " + src.doc(t.line, bs, be)):
        t.implicit = t.smoke = True
    pnames = [p for p in _IDENT.findall(params) if p not in _KEYWORDS]
    occs = _js_occs(jf, bs, be, pnames)
    if re.search(r"@ts-expect-error|\$Expect(?:Type|Error)", src.comments_in(bs, be)):
        occs.append(_Occ(bs, "real"))  # a compile-time assertion: tsc fails when the line has no type error
    if jf.lang != "js" and re.search(r"(?<![\w$.])(?:type\s+[A-Za-z_$][\w$]*\s*(?:<[^=;]*>)?\s*=|interface\s+[A-Za-z_$]|"
                                    r"(?:let|var)\s+[A-Za-z_$][\w$]*\s*!\s*:|\bas\s+any\s+as\b|\bsatisfies\b)", M[bs:be]):
        t.implicit = True  # a type-level test: the compiler checks it
    _fill(src, t, occs, bs, be, "js")
    if re.search(r"(?<![\w$.])(?:this|ctx|context|t|test)\s*\.\s*skip\s*\(", M[bs:be]):
        t.implicit = True
    if _JS_E2E.search(M, bs, be):
        t.implicit = True
    _swallowed(src, t, occs, bs, be, _JS_TRY, _JS_CATCH, lambda types: "catch", _js_fn_ranges(src, bs, be),
               _JS_QUIET, "js")
    nested = _js_fn_ranges(src, bs, be)
    for m in _JS_SLEEP.finditer(M, bs, be):
        word = next((g for g in m.groups() if g), "")
        if any(x < m.start() < y for x, y in nested):
            continue  # a callback that simulates slow work, not the test waiting
        if m.group().startswith("new"):
            pc = src.pairs.get(m.end() - 1, m.end())
            st = re.search(r"setTimeout\s*\(", M[m.end():pc])
            if not st:
                continue
            so = m.end() + st.end() - 1
            parts = src.args(so, src.pairs.get(so, so))
            if len(parts) < 2 or M[parts[-1][0]:parts[-1][1]].strip() in ("0", ""):
                continue  # setTimeout(resolve, 0) yields to the event loop
            word = "setTimeout in a Promise"
        if jf.fake_timers and word in ("setTimeout in a Promise", "setTimeout", "sleep", "delay", "wait"):
            continue
        if src.in_loop(bs, m.start(), "js"):
            continue
        t.sleeps.append((src.line(m.start()), word if " " in word else word + "()"))
        break
    for m in _JS_PRINT.finditer(M, bs, be):
        t.prints.append((src.line(m.start()), f"console.{m.group(1)}()"))
        break
    t.subject = _subject(src, bs, be, _JS_NOT_SUBJECT | jf.mocks)
    body = M[bs:be].strip()
    if re.fullmatch(r"(?:await\s+)?[\w$.]+\s*\(\s*\)\s*;?", body):
        t.dup = ""


def _js_no_prod(jf: _JsFile) -> bool:
    if not jf.specs:
        return False
    for spec, _ in jf.specs:
        s = spec[5:] if spec.startswith("node:") else spec
        if s.split("/")[0] in _JS_BUILTINS or _JS_FRAMEWORKS.match(spec):
            continue
        return False
    return not _JS_UNSURE.search(jf.src.m)


# --------------------------------------------------------------------------- Java / Kotlin

_J_TEST_ANN = {"Test", "ParameterizedTest", "RepeatedTest", "TestFactory", "TestTemplate"}
_J_ANN = re.compile(r"(?<![\w$`])@(?:(?:field|get|set|param|property|file|receiver|setparam|delegate):)?"
                    r"([A-Za-z_$][\w$.]*)")
_J_ASSERT = re.compile(r"(?<![\w$])_*(assert\w*|verify\w*|check\w+|expect\w*|fail|should\w*|andExpect\w*|then|"
                       r"until\w*|coVerify\w*|confirmVerified)\s*(?=[(<{])|(?<![\w$.])(require|error|check)\s*(?=\()|"
                       r"(?<![\w$.])(throw)\b|(?<![\w$.])(should\w+)\b")
_J_MOCK_ANN = {"Mock", "MockBean", "MockitoBean", "Spy", "SpyBean", "MockitoSpyBean", "MockK", "RelaxedMockK", "SpyK"}
_J_MOCK_DECL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*(?::\s*[\w<>?,. ]+)?=\s*(?:Mockito\s*\.\s*)?"
                          r"(?:mock|spy|mockk|spyk)\s*[<(]")
_J_FRAMEWORK_IMPORT = re.compile(r"^(?:java|javax|org\.junit|junit|org\.assertj|org\.mockito|org\.hamcrest|"
                                 r"com\.google\.common\.truth|org\.opentest4j|kotlin|io\.kotest|io\.mockk|"
                                 r"org\.jetbrains\.kotlin\.test)\.")
_J_LANG = {"Object", "String", "StringBuilder", "StringBuffer", "Integer", "Long", "Short", "Byte", "Double", "Float",
           "Boolean", "Character", "Number", "Math", "StrictMath", "System", "Thread", "Runnable", "Exception",
           "RuntimeException", "Error", "Throwable", "AssertionError", "IllegalArgumentException",
           "IllegalStateException", "NullPointerException", "IndexOutOfBoundsException",
           "ArrayIndexOutOfBoundsException", "StringIndexOutOfBoundsException", "UnsupportedOperationException",
           "ArithmeticException", "ClassCastException", "NumberFormatException", "InterruptedException",
           "CloneNotSupportedException", "ReflectiveOperationException", "ClassNotFoundException",
           "SecurityException", "StackOverflowError", "OutOfMemoryError", "Iterable", "Comparable", "CharSequence",
           "AutoCloseable", "Cloneable", "Enum", "Record", "Void", "Override", "Deprecated", "SuppressWarnings",
           "FunctionalInterface", "SafeVarargs", "ThreadLocal", "Appendable", "Readable"}
_J_UNSURE = re.compile(r"\b(?:ProcessBuilder|Runtime|Class\s*\.\s*forName|ClassLoader|getResource\w*|Files|Paths|"
                       r"File|HttpClient|URL|URI|ServiceLoader|Socket|getClass|reflect)\b")
_J_STATIC_OK = {"mock", "spy", "when", "given", "any", "anyString", "anyInt", "anyLong", "anyList", "anyMap",
                "anyBoolean", "anyDouble", "eq", "times", "never", "atLeast", "atLeastOnce", "atMost", "only",
                "doReturn", "doThrow", "doNothing", "doAnswer", "argThat", "isA", "same", "refEq", "inOrder",
                "lenient", "mockStatic", "mockConstruction", "reset", "clearInvocations", "timeout", "after", "is",
                "equalTo", "not", "hasItem", "hasItems", "contains", "containsString", "containsInAnyOrder",
                "hasSize", "empty", "closeTo", "notNullValue", "nullValue", "instanceOf", "startsWith", "endsWith",
                "greaterThan", "lessThan", "allOf", "anyOf", "hasEntry", "hasKey", "hasProperty", "everyItem",
                "sameInstance", "listOf", "mapOf", "setOf", "arrayOf", "emptyList", "emptyMap", "mutableListOf",
                "mutableMapOf", "mutableSetOf", "println", "print", "repeat", "run", "let", "also", "apply", "with",
                "lazy", "runBlocking", "runTest", "launch", "async", "delay", "withContext", "coEvery", "every",
                "mockk", "spyk", "slot", "just", "returns", "answers", "match", "capture", "to", "Pair", "require",
                "check", "error", "TODO", "maxOf", "minOf", "buildList", "buildString", "sequenceOf", "emptySet",
                "setUp", "tearDown", "super", "this", "assertAll", "requireNotNull", "checkNotNull", "takeIf",
                "use", "map", "filter", "first", "forEach", "toList", "size", "get", "put", "add", "of", "format",
                "valueOf", "toString", "equals", "hashCode"}
_J_CONTROL = {"if", "for", "while", "switch", "catch", "synchronized", "return", "new", "throw", "else", "try", "do",
              "when", "super", "this", "assert"}
_J_SNAP = {"toMatchSnapshot", "matchesSnapshot", "verifyAsJson", "verifySnapshot"}


def _j_trivial(src, name, parts, chain) -> bool:
    texts = [src.text[a:b].strip() for a, b in parts]
    if not texts:
        return False
    strs = [x[:1] == '"' for x in texts]
    if name in ("assertTrue", "assertFalse", "assertNull", "assertNotNull"):
        cands = [x for x, s in zip(texts, strs) if not s] if len(texts) == 2 else texts[:1]
        if not cands:
            return name == "assertNotNull"
        ok, v = _lit(cands[0])
        if not ok:
            return False
        return {"assertTrue": v is True, "assertFalse": v is False, "assertNull": v is None,
                "assertNotNull": v is not None}[name]
    if name in ("assertEquals", "assertSame", "assertArrayEquals", "assertIterableEquals", "assertNotEquals",
                "assertNotSame"):
        if len(texts) == 3:
            pair = texts[1:] if strs[0] else texts[:2]
        elif len(texts) == 2:
            pair = texts
        else:
            return False
        if name in ("assertNotEquals", "assertNotSame"):
            oka, va = _lit(pair[0])
            okb, vb = _lit(pair[1])
            return oka and okb and va != vb
        return _lit_or_same(pair[0], pair[1])
    if name == "assertThat":
        subj = texts[0]
        if len(texts) == 2:
            mm = re.match(r"^(?:is|equalTo|sameInstance)\s*\(\s*(?:equalTo\s*\()?(.*?)\)?\s*\)$", texts[1])
            return bool(mm) and _lit_or_same(subj, mm.group(1))
        if not chain:
            return False
        ok, v = _lit(subj)
        for cname, cargs, _ in chain:
            if cname in ("isEqualTo", "isSameAs") and cargs:
                if not _lit_or_same(subj, src.text[cargs[0]:cargs[1]]):
                    return False
            elif cname in ("isTrue", "isFalse", "isNull", "isNotNull"):
                if not ok or {"isTrue": v is not True, "isFalse": v is not False, "isNull": v is not None,
                              "isNotNull": v is None}[cname]:
                    return False
            elif cname in ("as", "describedAs", "withFailMessage", "overridingErrorMessage"):
                continue
            else:
                return False
        return True
    return False


class _JvmFile:
    def __init__(self, text, lang, rel):
        self.lang, self.rel = lang, rel
        self.src = _Src(text, lang)
        src, M = self.src, self.src.m
        self.imports = [(m.group(1), m.group(2) or "") for m in
                        re.finditer(r"(?m)^\s*import\s+(?:static\s+)?([\w.`]+)(\.\*)?", M)]
        self.mockito = bool(re.search(r"org\.mockito|io\.mockk|\bMockito\b", M))
        self.blocks = self._annotation_blocks()
        self.classes = []
        for m in re.finditer(r"(?<![\w$.])(?:class|interface|object|enum|record)\s+(?!class\b)([A-Za-z_$][\w$]*)", M):
            bo = M.find("{", m.end())
            semi = M.find(";", m.end())
            if bo < 0 or (0 <= semi < bo and lang == "java" and "(" not in M[m.end():semi]):
                continue
            # skip constructor parameter lists (Kotlin) and generics before the body
            k = m.end()
            while True:
                bo = M.find("{", k)
                p = M.find("(", k)
                if 0 <= p < bo and p in src.pairs:
                    k = src.pairs[p] + 1
                    continue
                break
            if bo < 0 or bo not in src.pairs:
                continue
            head = M[m.end():bo]
            ext = re.search(r"\bextends\s+([\w$.]+)|:\s*([\w$.]+)\s*\(", head)
            self.classes.append({"name": m.group(1), "a": bo, "b": src.pairs[bo], "line": src.line(m.start()),
                                 "extends": (ext.group(1) or ext.group(2)) if ext else ""})
        self.class_names = {c["name"] for c in self.classes}
        self.mocks = {m.group(1) for m in _J_MOCK_DECL.finditer(M)}
        self.methods = self._methods()
        for b in self.blocks:
            if b["names"] & _J_MOCK_ANN and b.get("field"):
                self.mocks.add(b["field"])
        for _ in range(2):
            self.mocks |= {m.group(1) for m in re.finditer(
                r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*(?::\s*[\w<>?,. ]+)?=\s*([A-Za-z_$][\w$]*)\s*\.", M)
                if m.group(2) in self.mocks}
        helper_bodies = {x["name"]: (x["bs"], x["be"]) for x in self.methods if not x["test"] and x["bs"] is not None}
        found = {n for n, (a, b) in helper_bodies.items() if _J_ASSERT.search(M, a, b)}
        for _ in range(5):
            more = {n for n, (a, b) in helper_bodies.items() if n not in found and _calls_any(src, found, a, b)}
            if not more:
                break
            found |= more
        self.helpers = found
        self.defined = {x["name"] for x in self.methods}
        self.support_static = any(not _J_FRAMEWORK_IMPORT.match(i + ".") and (star or _SUPPORT.search(i))
                                  for i, star in self.imports)

    def _annotation_blocks(self):
        src, M = self.src, self.src.m
        blocks, cur = [], None
        for m in _J_ANN.finditer(M):
            if M[max(0, m.start() - 7):m.start()].rstrip().endswith("@interface"):
                continue
            end = m.end()
            k = _ws(M, end)
            args = None
            if k < len(M) and M[k] == "(" and k in src.pairs:
                args = (k + 1, src.pairs[k])
                end = src.pairs[k] + 1
            name = m.group(1).split(".")[-1]
            if cur and not M[cur["end"]:m.start()].strip():
                cur["names"].add(name)
                cur["args"][name] = args
                cur["end"] = end
            else:
                cur = {"start": m.start(), "end": end, "names": {name}, "args": {name: args}}
                blocks.append(cur)
        for b in blocks:
            rest = M[b["end"]:b["end"] + 600]
            p = rest.find("(")
            stop = re.search(r"[;{}=]", rest)
            prop = re.match(r"\s*(?:(?:private|protected|public|internal|lateinit|override|open|final)\s+)*"
                            r"(?:val|var)\s+([\w$]+)", rest)
            if prop:
                b["field"] = prop.group(1)
                continue
            decl = re.match(r"\s*(?:(?:public|protected|private|internal|static|final|abstract|open|override|"
                            r"lateinit|sealed|data|inner)\s+)*(?:class|interface|object|enum|record)\s+([\w$]+)", rest)
            if decl:
                b["class"] = decl.group(1)
            elif stop and (p < 0 or stop.start() < p):
                fm = re.search(r"(?:val|var)\s+([\w$]+)|([\w$]+)\s*(?::[^;={}]*)?$", rest[:stop.start()])
                if fm:
                    b["field"] = fm.group(1) or fm.group(2)
            elif p >= 0:
                b["paren"] = b["end"] + p
        return blocks

    def _methods(self):
        src, M = self.src, self.src.m
        found = {}
        kotlin = self.lang == "kotlin"
        rx = re.compile(r"(?<![\w$])fun\s+(?:<[^>]*>\s*)?(?:[\w$.]+\.)?([A-Za-z_$][\w$]*|`\s*`)\s*\(") if kotlin \
            else re.compile(r"(?<![\w$.@])([A-Za-z_$][\w$]*)\s*\(")
        for m in rx.finditer(M):
            name = m.group(1)
            if name in _J_CONTROL:
                continue
            po = m.end() - 1
            pc = src.pairs.get(po)
            if pc is None:
                continue
            if not kotlin:
                pre = M[max(0, m.start() - 60):m.start()]
                if not re.search(r"[\w$>\]]\s+$", pre) or re.search(r"\b(?:new|return|throw|else|case)\s+$", pre):
                    continue
                after = re.match(r"\s*(?:throws\s+[\w$.,\s]+)?\{", M[pc + 1:pc + 300])
                if not after:
                    continue
                bo = pc + 1 + after.end() - 1
            else:
                after = re.match(r"\s*(?::\s*[\w$.<>?, ]+)?\s*([{=])", M[pc + 1:pc + 300])
                if not after:
                    continue
                bo = pc + 1 + after.start(1)
                if after.group(1) == "=":
                    k = _ws(M, bo + 1)
                    line_end = M.find("\n", k)
                    line_end = len(M) if line_end < 0 else line_end
                    br = M.find("{", k, line_end)
                    if br < 0:
                        found[m.start()] = self._method(m, name, po, k, line_end, expr=True)
                        continue
                    bo = br
            bc = src.pairs.get(bo)
            if bc is None:
                continue
            found[m.start()] = self._method(m, name, po, bo + 1, bc)
        return list(found.values())

    def _method(self, m, name, po, bs, be, expr=False):
        src = self.src
        if name.startswith("`"):
            name = src.text[m.start(1) + 1:m.end(1) - 1].strip()
        block = next((b for b in reversed(self.blocks) if b.get("paren") == po and b["end"] <= m.start()), None)
        names = block["names"] if block else set()
        return {"name": name, "pos": m.start(), "bs": bs, "be": be, "block": block,
                "test": bool(names & _J_TEST_ANN), "line": src.line(block["start"] if block else m.start()),
                "name_line": src.line(m.start()), "expr": expr}


def _analyze_jvm(text, path, base, rel, lang):
    jf = _JvmFile(text, lang, rel)
    src, M = jf.src, jf.src.m
    tests, extra, bodies = [], [], {}
    skipped_classes = []
    for b in jf.blocks:
        if b.get("class") and b["names"] & {"Disabled", "Ignore"}:
            cls = next((c for c in jf.classes if c["name"] == b["class"]), None)
            if cls:
                skipped_classes.append(cls)
                if _j_noreason(src, b, b["names"] & {"Disabled", "Ignore"}):
                    extra.append(_finding(rel, src.line(b["start"]), b["class"], "skipped-without-reason",
                                          "is skipped with no reason", _FIX_SKIP["java"]))
    for meth in jf.methods:
        if not meth["test"]:
            continue
        cls = max((c for c in jf.classes if c["a"] < meth["pos"] < c["b"]), key=lambda c: c["a"], default=None)
        cname = cls["name"] if cls else ""
        t = _T(name=f"{cname}.{meth['name']}" if cname else meth["name"], line=meth["name_line"], bare=meth["name"],
               scope=str(cls["a"]) if cls else "")
        blk = meth["block"]
        skip_ann = blk["names"] & {"Disabled", "Ignore"}
        if skip_ann:
            t.skipped = True
            if _j_noreason(src, blk, skip_ann):
                t.skip_line = src.line(blk["start"])
        if any(c["a"] < meth["pos"] < c["b"] for c in skipped_classes):
            t.skipped = True
        bs, be = meth["bs"], meth["be"]
        bodies[id(t)] = M[bs:be]
        _jvm_test(jf, t, meth, cls, bs, be)
        tests.append(t)
    if tests and lang == "java" and _java_no_prod(jf):
        extra.append(_no_prod(rel, tests[0].line))
    return tests, extra, bodies


def _j_noreason(src, blk, names) -> bool:
    for n in names:
        a = blk["args"].get(n)
        if a is None:
            return True
        inner = src.text[a[0]:a[1]].strip()
        if not inner or re.fullmatch(r"(?:value\s*=\s*)?\"\s*\"", inner):
            return True
    return False


_J_TRY = re.compile(r"(?<![\w$.])try\s*(?:\([^{]*?\)\s*)?\{")
_J_CATCH = re.compile(r"\s*catch\s*\(([^)]*)\)\s*\{")
_J_BROAD = {"Throwable", "Error", "AssertionError", "AssertionFailedError", "ComparisonFailure",
            "MultipleFailuresError"}
_J_QUIET = re.compile(r"(?:System\s*\.\s*(?:out|err)\s*\.\s*print\w*|[\w$]+\s*\.\s*printStackTrace|"
                      r"(?:log|logger|LOG|LOGGER|Log|Logger)\s*\.\s*\w+|(?<![\w$.])println|(?<![\w$.])print)\s*\(")


def _j_broad(types):
    names = {x.split(".")[-1] for x in re.findall(r"[A-Za-z_$][\w$.]*", types)}
    hit = names & _J_BROAD
    return f"catch {min(hit)}" if hit else ""


def _jvm_test(jf: _JvmFile, t: _T, meth, cls, bs, be):
    src, M = jf.src, jf.src.m
    if not M[bs:be].strip():
        t.empty = True
        return
    if _smoke(meth["name"], src.doc(meth["line"], bs, be)):
        t.implicit = t.smoke = True
    blk = meth["block"] or {"args": {}}
    targs = blk["args"].get("Test")
    occs = []
    if targs and re.search(r"\bexpected\s*=", M[targs[0]:targs[1]]):
        occs.append(_Occ(bs, "real"))
    for m in _J_ASSERT.finditer(M, bs, be):
        name = m.group(1) or m.group(2) or m.group(3) or m.group(4)
        occs.append(_Occ(m.start(), _j_kind(jf, m, name, bs, be), src.call_text(m.start())))
    if jf.helpers:
        for m in re.finditer(r"(?<![\w$.])(?:this\s*\.\s*)?(" + "|".join(re.escape(x) for x in sorted(jf.helpers)) +
                             r")\s*\(", M[bs:be]):
            occs.append(_Occ(bs + m.start(), "real"))
    unknown_base = bool(cls and cls["extends"] and cls["extends"].split(".")[-1] not in jf.class_names)
    for m in re.finditer(r"(?<![\w$.@])([a-z_$][\w$]*)\s*[({]", M[bs:be]):
        w = m.group(1)
        if w in _J_CONTROL or w in _J_STATIC_OK or w in jf.defined or re.match(
                r"(?:assert|verify|check|expect|fail|should|then|until)", w):
            continue
        if unknown_base or jf.support_static or jf.lang == "kotlin":
            occs.append(_Occ(bs + m.start(), "real"))
            break
    for m in re.finditer(r"(?<![\w$.])([A-Z][\w$]*)\s*\.\s*([a-z][\w$]*)\s*\(", M[bs:be]):
        if _SUPPORT.search(m.group(1)) and not _J_FRAMEWORK_IMPORT.match(m.group(1) + "."):
            occs.append(_Occ(bs + m.start(), "real"))
    occs.sort(key=lambda o: o.pos)
    _fill(src, t, occs, bs, be, "java")
    fr = [(a, src.pairs.get(a, a)) for a in (bs + mm.end() - 1 for mm in re.finditer(r"->\s*\{", M[bs:be]))]
    _swallowed(src, t, occs, bs, be, _J_TRY, _J_CATCH, _j_broad, fr, _J_QUIET, "java")
    sleep = re.compile(r"Thread\s*\.\s*sleep\s*\(|TimeUnit\s*\.\s*\w+\s*\.\s*sleep\s*\(|SystemClock\s*\.\s*sleep\s*\(")
    for m in sleep.finditer(M, bs, be):
        if not src.in_loop(bs, m.start(), "java"):
            t.sleeps.append((src.line(m.start()), _clip(re.sub(r"\s+", "", M[m.start():m.end()]) + ")")))
            break
    if jf.lang == "kotlin" and "runBlocking" in M[bs:be] and not t.sleeps:
        for m in re.finditer(r"(?<![\w$.])delay\s*\(", M[bs:be]):
            if not src.in_loop(bs, bs + m.start(), "kotlin"):
                t.sleeps.append((src.line(bs + m.start()), "delay() in runBlocking"))
                break
    pr = re.compile(r"System\s*\.\s*(?:out|err)\s*\.\s*print\w*\s*\(|\.\s*printStackTrace\s*\(\s*\)" +
                    (r"|(?<![\w$.])print(?:ln)?\s*\(" if jf.lang == "kotlin" else ""))
    for m in pr.finditer(M, bs, be):
        t.prints.append((src.line(m.start()), re.sub(r"\s+", "", M[m.start():m.end()]).rstrip("(") + "()"))
        break
    t.subject = _subject(src, bs, be, _J_STATIC_OK | _J_CONTROL | jf.mocks | {"of", "get"},
                         re.compile(r"(?<![\w$])([a-z_][\w$]*)\s*\("))
    blk_text = src.norm(blk["start"], blk["end"]) if meth["block"] else ""
    t.dup = blk_text + "#" + src.norm(bs, be)


def _j_kind(jf: _JvmFile, m, name, bs, be):
    src, M = jf.src, jf.src.m
    if name in ("verify", "verifyNoInteractions", "verifyNoMoreInteractions", "verifyZeroInteractions",
                "confirmVerified") or name.startswith("coVerify"):
        pre = M[max(0, m.start() - 12):m.start()]
        if re.search(r"Approvals\s*\.\s*$", pre):
            return "snapshot"
        return "mock" if jf.mockito else "real"
    if name in _J_SNAP:
        return "snapshot"
    k = _ws(M, m.end())
    if k >= len(M) or M[k] != "(" or k not in src.pairs:
        return "real"
    close = src.pairs[k]
    chain = src.chain(close + 1)
    if any(c[0] in _J_SNAP for c in chain):
        return "snapshot"
    if name == "then":
        if any(c[0] == "should" for c in chain) and jf.mockito:
            return "mock"
        return "real"
    if not name.startswith("assert"):
        return "real"
    parts = src.args(k, close)
    if _j_trivial(src, name, parts, chain):
        return "trivial"
    subj = M[k + 1:close] if name == "assertThat" else " ".join(
        M[a:b] for a, b in parts if src.text[a:b].strip()[:1] != '"')
    r = _roots(subj, drop=_J_STATIC_OK)
    if r and r <= jf.mocks:
        return "mock"
    return "real"


def _java_no_prod(jf: _JvmFile) -> bool:
    M = jf.src.m
    names = set()
    for imp, star in jf.imports:
        if not _J_FRAMEWORK_IMPORT.match(imp + "."):
            return False
        names.update(imp.split("."))
    if _J_UNSURE.search(M):
        return False
    body = re.sub(r"(?m)^\s*(?:package|import)\b.*$", "", M)
    declared = jf.class_names | set(re.findall(r"<\s*([A-Z])\s*[,>]", body))
    for m in re.finditer(r"(?<![\w$.@])([A-Z][\w$]*)\b", body):
        w = m.group(1)
        if w in names or w in declared or w in _J_LANG or w.isupper():
            continue
        return False
    for c in jf.classes:
        if c["extends"] and c["extends"] not in jf.class_names:
            return False
    return True


# --------------------------------------------------------------------------- Go

_GO_TEST = re.compile(r"(?m)^func\s+(Test\w*)\s*\(\s*([A-Za-z_]\w*)\s+\*\s*testing\s*\.\s*T\s*\)")
_GO_FUNC = re.compile(r"(?m)^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)?\(")
_GO_FAIL = re.compile(r"(?<![\w.])[A-Za-z_]\w*\s*\.\s*(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\s*\(|"
                      r"(?<![\w.])(?:assert|require)\s*\.\s*[A-Z]\w*\s*\(|(?<![\w.])panic\s*\(|"
                      r"(?<![\w])(?:[A-Za-z_]\w*\s*\.\s*)?(?:Expect|Eventually|Consistently|Ω)\s*\(")


def _analyze_go(text, path, base, rel):
    src = _Src(text, "go")
    M = src.m
    funcs = {}
    for m in _GO_FUNC.finditer(M):
        po = m.end() - 1
        pc = src.pairs.get(po)
        bo = M.find("{", pc) if pc else -1
        if bo >= 0 and bo in src.pairs:
            funcs[m.group(1)] = (m, bo + 1, src.pairs[bo])
    helpers = {n for n, (_, a, b) in funcs.items() if _GO_FAIL.search(M, a, b) and not n.startswith("Test")}
    for _ in range(5):
        more = {n for n, (_, a, b) in funcs.items() if n not in helpers and _calls_any(src, helpers, a, b)}
        if not more:
            break
        helpers |= more
    mocks = {m.group(1) for m in re.finditer(
        r"(?<![\w.])([A-Za-z_]\w*)\s*:?=\s*(?:(?:\w+\.)?NewMock\w*\s*\(|&\s*(?:\w+\.)?Mock\w*\s*\{|"
        r"new\s*\(\s*(?:\w+\.)?Mock\w*\s*\))", M)}
    tests, bodies = [], {}
    for m in _GO_TEST.finditer(M):
        name, tv = m.group(1), m.group(2)
        if len(name) > 4 and name[4].islower():
            continue
        bo = M.find("{", m.end())
        if bo < 0 or bo not in src.pairs:
            continue
        bs, be = bo + 1, src.pairs[bo]
        t = _T(name=name, line=src.line(m.start()), bare=name)
        bodies[id(t)] = M[bs:be]
        _go_test(src, t, tv, bs, be, helpers, mocks)
        tests.append(t)
    extra = []
    if tests and _go_no_prod(src, path, base):
        extra.append(_no_prod(rel, tests[0].line))
    return tests, extra, bodies


def _go_test(src: _Src, t: _T, tv, bs, be, helpers, mocks):
    M = src.m
    if not M[bs:be].strip():
        t.empty = True
        return
    T = re.escape(tv)
    if _smoke(t.name, src.doc(t.line, bs, be, above=4)):
        t.implicit = t.smoke = True
    occs = []
    objs = {m.group(1) for m in re.finditer(r"(?<![\w.])([A-Za-z_]\w*)\s*:?=\s*[A-Za-z_]\w*\s*\.\s*New\s*\(\s*" + T +
                                            r"\s*\)", M[bs:be])}
    for m in re.finditer(r"(?<![\w.])([A-Za-z_]\w*)\s*\.\s*(Error|Errorf|Fatal|Fatalf|Fail|FailNow)\s*\(", M[bs:be]):
        occs.append(_Occ(bs + m.start(), "real"))
    for m in re.finditer(r"(?<![\w.])(assert|require|" + "|".join(re.escape(o) for o in sorted(objs) or ["\x00"]) +
                         r")\s*\.\s*([A-Z]\w*)\s*\(", M[bs:be]):
        k = bs + m.end() - 1
        parts = src.args(k, src.pairs.get(k, k))
        if m.group(1) in ("assert", "require") and parts:
            parts = parts[1:]
        occs.append(_Occ(bs + m.start(), _go_kind(src, m.group(2), parts, mocks, tv), src.call_text(bs + m.start())))
    for m in re.finditer(r"(?<![\w.])(?:[A-Za-z_]\w*\s*\.\s*)?(?:Expect|Eventually|Consistently|Ω)\s*\(|"
                         r"(?<![\w.])panic\s*\(", M[bs:be]):
        occs.append(_Occ(bs + m.start(), "real"))
    for m in re.finditer(r"(?<![\w.])([A-Za-z_]\w*)\s*\.\s*(Assert\w*|EXPECT)\s*\(", M[bs:be]):
        if m.group(1) in mocks or m.group(1) == "mock":
            occs.append(_Occ(bs + m.start(), "mock"))
    for m in re.finditer(r"(?<![\w])((?:[A-Za-z_]\w*\s*\.\s*)?[A-Za-z_]\w*)\s*\(\s*" + T + r"\s*[,)]", M[bs:be]):
        callee = re.sub(r"\s+", "", m.group(1))
        last = callee.split(".")[-1]
        if callee.split(".")[0] in ("assert", "require") or callee.split(".")[0] in objs:
            continue
        if last.startswith("NewMock"):
            occs.append(_Occ(bs + m.start(), "mock"))
        elif last.startswith("New"):
            continue
        elif callee.split(".")[0] in mocks:
            continue
        else:
            occs.append(_Occ(bs + m.start(), "real"))
    if helpers:
        for m in re.finditer(r"(?<![\w.])(?:" + "|".join(re.escape(h) for h in sorted(helpers)) + r")\s*\(",
                             M[bs:be]):
            occs.append(_Occ(bs + m.start(), "real"))
    occs.sort(key=lambda o: o.pos)
    _fill(src, t, occs, bs, be, "go")
    for m in re.finditer(r"(?<![\w.])" + T + r"\s*\.\s*(Skip|SkipNow|Skipf)\s*\(", M[bs:be]):
        pos = bs + m.start()
        k = bs + m.end() - 1
        empty = not M[k + 1:src.pairs.get(k, k)].strip()
        if src.top_level(bs, pos):
            t.skipped = True
            if empty or m.group(1) == "SkipNow":
                t.skip_line = src.line(pos)
        else:
            t.implicit = True
        break
    for m in re.finditer(r"(?<![\w.])time\s*\.\s*Sleep\s*\(", M[bs:be]):
        if not src.in_loop(bs, bs + m.start(), "go"):
            t.sleeps.append((src.line(bs + m.start()), "time.Sleep()"))
            break
    for m in re.finditer(r"(?<![\w.])(fmt\s*\.\s*Print(?:ln|f)?|print(?:ln)?|log\s*\.\s*Print(?:ln|f)?)\s*\(",
                         M[bs:be]):
        t.prints.append((src.line(bs + m.start()), re.sub(r"\s+", "", m.group(1)) + "()"))
        break
    t.subject = _subject(src, bs, be, {"Run", "Errorf", "Error", "Fatal", "Fatalf", "New", "Equal", "NoError",
                                       "Helper", "Parallel", "Sprintf", "Println", "Printf", "Sleep", "Skip", "range",
                                       "append", "make", "len", "func", "Diff", "DeepEqual", "Background", "On",
                                       "Return", "Cleanup", "TempDir"} | mocks | {tv},
                         re.compile(r"(?<![\w])([A-Za-z_]\w*)\s*\("))
    t.dup = re.sub(r"\b" + T + r"\b", "t", src.norm(bs, be))


def _go_kind(src, method, parts, mocks, tv):
    texts = [src.text[a:b].strip() for a, b in parts]
    if method in ("Equal", "EqualValues", "Exactly", "Same") and len(texts) >= 2 and _lit_or_same(texts[0], texts[1]):
        return "trivial"
    if method in ("True", "False", "Nil", "NotNil") and texts:
        ok, v = _lit(texts[0])
        if ok and {"True": v is True, "False": v is False, "Nil": v is None, "NotNil": v is not None}[method]:
            return "trivial"
    r = _roots(" ".join(src.m[a:b] for a, b in parts), drop={tv})
    if r and r <= mocks:
        return "mock"
    return "real"


def _go_no_prod(src: _Src, path: Path, base: Path) -> bool:
    M, T = src.m, src.text
    pkg = re.search(r"(?m)^package\s+(\w+)", M)
    if not pkg:
        return False
    imports = [T[m.start(1) + 1:m.end(1) - 1] for m in re.finditer(r"(?m)^\s*(?:import\s+)?(?:[\w.]+\s+)?(\"[^\"]*\")",
                                                                   M[:M.find("func ") if "func " in M else len(M)])]
    module = ""
    d = path.parent
    for _ in range(20):
        gm = d / "go.mod"
        if gm.is_file():
            mm = re.search(r"(?m)^module\s+(\S+)", gm.read_text(errors="replace"))
            module = mm.group(1) if mm else ""
            break
        if d == base or d.parent == d:
            break
        d = d.parent
    for imp in imports:
        first = imp.split("/")[0]
        if "." in first or (module and imp.startswith(module)) or imp in ("os/exec", "net/http", "os", "embed",
                                                                          "plugin", "net", "io/fs"):
            return False
    if pkg.group(1).endswith("_test"):
        return True
    names, helpers_ids = set(), set()
    for p in sorted(path.parent.glob("*.go")):
        if p == path:
            continue
        try:
            other = p.read_text(errors="replace")
        except OSError:
            return False
        om = _Src(other, "go").m
        pm = re.search(r"(?m)^package\s+(\w+)", om)
        if not pm or pm.group(1) != pkg.group(1):
            continue
        ids = set(re.findall(r"(?m)^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", om))
        ids |= set(re.findall(r"(?m)^(?:type|var|const)\s+([A-Za-z_]\w*)", om))
        for blk in re.finditer(r"(?ms)^(?:type|var|const)\s*\((.*?)^\)", om):
            ids |= set(re.findall(r"(?m)^\s*([A-Za-z_]\w*)", blk.group(1)))
        (helpers_ids if p.name.endswith("_test.go") else names).update(ids)
    words = set(re.findall(r"[A-Za-z_]\w*", M))
    return not (words & helpers_ids or words & names)


# --------------------------------------------------------------------------- PHP

_P_ASSERT = re.compile(r"(?<![\w$])_*(assert\w*|expect\w*|verify\w*|check\w*|fail|should\w*)\s*\(|(?<![\w$])(throw)\b")
_P_KNOWN = {"createMock", "createStub", "getMockBuilder", "once", "never", "any", "exactly", "atLeastOnce", "atLeast",
            "atMost", "returnValue", "returnCallback", "onConsecutiveCalls", "identicalTo", "equalTo", "callback",
            "markTestSkipped", "markTestIncomplete", "getName", "name", "setUp", "tearDown", "createPartialMock",
            "createConfiguredMock", "getMockForAbstractClass", "returnSelf", "throwException", "at", "logicalAnd",
            "logicalOr", "isType", "isInstanceOf", "stringContains", "anything", "dataName", "get", "post", "put",
            "patch", "delete", "json", "getJson", "postJson", "putJson", "patchJson", "deleteJson", "actingAs",
            "withHeaders", "withHeader", "withoutMiddleware", "withoutExceptionHandling", "app", "mock",
            "partialMock", "spy", "instance", "swap", "seed", "travel", "travelTo", "freezeTime", "withSession",
            "from", "followingRedirects", "call", "refreshApplication", "prophesize", "withCookie", "be"}
_P_TRY = re.compile(r"(?<![\w$])try\s*\{")
_P_CATCH = re.compile(r"\s*catch\s*\(([^)]*)\)\s*\{")
_P_QUIET = re.compile(r"(?<![\w$>:\\])(?:var_dump|print_r|error_log|dump|printf|fwrite|var_export)\s*\(|"
                      r"(?<![\w$])(?:echo|print)\b[^;]*;|\$this\s*->\s*logger\s*->\s*\w+\s*\(|"
                      r"\$\w+\s*->\s*(?:log|info|debug|warning|error|notice)\s*\(")
_P_MOCK_DECL = re.compile(r"\$(this\s*->\s*\w+|\w+)\s*=\s*(?:\$this\s*->\s*(?:createMock|createStub|getMockBuilder|"
                          r"createPartialMock|createConfiguredMock|getMockForAbstractClass|mock|partialMock|spy|"
                          r"prophesize)|Mockery\s*::\s*(?:mock|spy|namedMock))\s*\(")


def _analyze_php(text, path, base, rel):
    src = _Src(text, "php")
    M, T = src.m, src.text
    uses = set(re.findall(r"(?m)^\s*use\s+\\?([\w\\]+)\s*;", M))
    namespaced = bool(re.search(r"(?m)^\s*namespace\s+[\w\\]+", M))
    mocks = {re.sub(r"\s+", "", m.group(1)) for m in _P_MOCK_DECL.finditer(M)}
    classes = []
    for m in re.finditer(r"(?<![\w$])(?:(?:abstract|final|readonly)\s+)*class\s+(\w+)(?:\s+extends\s+([\\\w]+))?", M):
        bo = M.find("{", m.end())
        if bo >= 0 and bo in src.pairs:
            classes.append({"name": m.group(1), "extends": (m.group(2) or "").split("\\")[-1], "a": bo,
                            "b": src.pairs[bo]})
    methods = []
    for m in re.finditer(r"(?<![\w$])function\s+&?\s*(\w+)\s*\(", M):
        po = m.end() - 1
        pc = src.pairs.get(po)
        if pc is None:
            continue
        after = re.match(r"\s*(?::\s*\??[\w\\|]+\s*)?\{", M[pc + 1:pc + 200])
        if not after:
            continue
        bo = pc + 1 + after.end() - 1
        if bo not in src.pairs:
            continue
        cls = max((c for c in classes if c["a"] < m.start() < c["b"]), key=lambda c: c["a"], default=None)
        head_start = max(M.rfind(";", 0, m.start()), M.rfind("{", 0, m.start()), M.rfind("}", 0, m.start()))
        head = T[head_start + 1:m.start()]
        methods.append({"name": m.group(1), "pos": m.start(), "bs": bo + 1, "be": src.pairs[bo], "cls": cls,
                        "doc_test": bool(re.search(r"@test\b", head)),
                        "attr_test": bool(re.search(r"#\[\s*\\?(?:[\w\\]+\\)?Test\s*[\](,]", head))})
    helper_bodies = {x["name"]: (x["bs"], x["be"]) for x in methods}
    found = {n for n, (a, b) in helper_bodies.items() if _P_ASSERT.search(M, a, b)}
    for _ in range(5):
        more = {n for n, (a, b) in helper_bodies.items() if n not in found and _calls_any(
            src, found, a, b, prefix=r"(?:\$this\s*->\s*|self\s*::\s*|static\s*::\s*|(?<![\w$>:\\]))")}
        if not more:
            break
        found |= more
    ctx = {"src": src, "mocks": mocks, "helpers": found, "defined": set(helper_bodies), "uses": uses,
           "namespaced": namespaced}
    tests, bodies, extra = [], {}, []
    for meth in methods:
        cls = meth["cls"]
        if not cls or not re.search(r"(?i)test", cls["extends"] or ""):
            continue
        if not (meth["name"].lower().startswith("test") or meth["doc_test"] or meth["attr_test"]):
            continue
        t = _T(name=f"{cls['name']}.{meth['name']}", line=src.line(meth["pos"]), bare=meth["name"],
               scope=str(cls["a"]))
        bodies[id(t)] = M[meth["bs"]:meth["be"]]
        _php_test(ctx, t, meth["bs"], meth["be"])
        tests.append(t)
    in_class = [(c["a"], c["b"]) for c in classes]
    describes = []
    for m in re.finditer(r"(?<![\w$>:\\])(it|test|describe|todo)\s*\(", M):
        if any(a < m.start() < b for a, b in in_class) or re.search(r"function\s+$", M[max(0, m.start() - 12):
                                                                                       m.start()]):
            continue
        o = m.end() - 1
        c = src.pairs.get(o)
        if c is None:
            continue
        args = src.args(o, c)
        if not args or m.group(1) == "todo":
            continue
        name = _js_name(src, *args[0])
        fn = None
        for x, y in args[1:]:
            fn = _php_fn(src, x, y)
            if fn:
                break
        chain = src.chain(c + 1, sep="->")
        if m.group(1) == "describe":
            if fn:
                describes.append((fn[0], fn[1]))
            continue
        cnames = [x[0] for x in chain]
        if "todo" in cnames:
            continue
        t = _T(name=name, line=src.line(m.start()), bare=name)
        for cname, cargs, _ in chain:
            if cname == "skip":
                t.skipped = True
                if cargs is None or not M[cargs[0]:cargs[1]].strip():
                    t.skip_line = src.line(m.start())
                elif T[cargs[0]:cargs[1]].strip()[:1] not in "'\"":
                    t.skipped, t.implicit = False, True
        if fn is None:
            t.body = False
            tests.append(t)
            continue
        bs, be = fn
        inside = [d for d in describes if d[0] <= m.start() < d[1]]
        t.scope = str(max(d[0] for d in inside)) if inside else ""
        bodies[id(t)] = M[bs:be]
        chain_assert = any(re.match(r"(?:throws\w*|expect\w*|assert\w*|to[A-Z]\w*)", x) for x in cnames)
        _php_test(ctx, t, bs, be, chain_assert)
        tests.append(t)
    return tests, extra, bodies


def _php_fn(src: _Src, a, b):
    M = src.m
    i = _ws(M, a)
    m = re.compile(r"(?:static\s+)?function\s*\(").match(M, i)
    if m:
        pc = src.pairs.get(m.end() - 1)
        if pc is None:
            return None
        k = pc + 1
        um = re.compile(r"\s*use\s*\(").match(M, k)
        if um:
            k = src.pairs.get(k + um.end() - 1, k) + 1
        bo = M.find("{", k, b)
        bc = src.pairs.get(bo) if bo >= 0 else None
        return (bo + 1, bc) if bc and bc <= b else None
    m = re.compile(r"(?:static\s+)?fn\s*\(").match(M, i)
    if m:
        pc = src.pairs.get(m.end() - 1)
        j = M.find("=>", pc or 0, b)
        return (j + 2, b) if pc and j > 0 else None
    return None


def _php_test(ctx, t: _T, bs, be, chain_assert=False):
    src, M = ctx["src"], ctx["src"].m
    if not M[bs:be].strip():
        if not chain_assert:
            t.empty = True
        return
    if _smoke(t.bare, (t.bare if " " in t.bare else "") + " " + src.doc(t.line, bs, be, above=4)):
        t.implicit = t.smoke = True
    occs = [_Occ(bs, "real")] if chain_assert else []
    for m in _P_ASSERT.finditer(M, bs, be):
        name = m.group(1) or m.group(2)
        occs.append(_Occ(m.start(), _php_kind(ctx, m, name), src.call_text(m.start())))
    if ctx["helpers"]:
        rx = re.compile(r"(?:\$this\s*->\s*|self\s*::\s*|static\s*::\s*|(?<![\w$>:\\]))(?:" +
                        "|".join(re.escape(x) for x in sorted(ctx["helpers"])) + r")\s*\(")
        occs += [_Occ(m.start(), "real") for m in rx.finditer(M, bs, be)]
    for m in re.finditer(r"(?:\$this\s*->|self\s*::|static\s*::|parent\s*::)\s*(\w+)\s*\(", M[bs:be]):
        w = m.group(1)
        if w not in ctx["defined"] and w not in _P_KNOWN and not re.match(r"(?i)(assert|expect|fail|mark)", w):
            occs.append(_Occ(bs + m.start(), "real"))
    occs.sort(key=lambda o: o.pos)
    _fill(src, t, occs, bs, be, "php")
    for m in re.finditer(r"(?<![\w$])(markTestSkipped|markTestIncomplete)\s*\(", M[bs:be]):
        pos = bs + m.start()
        k = bs + m.end() - 1
        if src.top_level(bs, pos):
            t.skipped = True
            if not M[k + 1:src.pairs.get(k, k)].strip():
                t.skip_line = src.line(pos)
        else:
            t.implicit = True
        break

    def broad(types):
        for x in types.split("|"):
            x = x.strip().split()[0] if x.strip() else ""
            last = x.split("\\")[-1]
            if last in ("AssertionFailedError", "ExpectationFailedException"):
                return f"catch {last}"
            if last in ("Throwable", "Exception", "RuntimeException") and (
                    x.startswith("\\") or not ctx["namespaced"] or last in {u.split("\\")[-1] for u in ctx["uses"]}):
                return f"catch {x}"
        return ""

    fr = []
    for mm in re.finditer(r"(?<![\w$])function\s*\(", M[bs:be]):
        p = bs + mm.end() - 1
        bo = M.find("{", src.pairs.get(p, p))
        if bo >= 0:
            fr.append((bo, src.pairs.get(bo, bo)))
    _swallowed(src, t, occs, bs, be, _P_TRY, _P_CATCH, broad, fr, _P_QUIET, "php")
    for m in re.finditer(r"(?<![\w$>:\\])(sleep|usleep|time_nanosleep|time_sleep_until)\s*\(", M[bs:be]):
        if not src.in_loop(bs, bs + m.start(), "php"):
            t.sleeps.append((src.line(bs + m.start()), m.group(1) + "()"))
            break
    for m in re.finditer(r"(?<![\w$>:\\])(var_dump|print_r|var_export|dump|dd|ray)\s*\(|(?<![\w$>:\\])(echo|print)\b",
                         M[bs:be]):
        w = m.group(1) or m.group(2)
        if w in ("print_r", "var_export"):
            k = bs + m.end() - 1
            if re.search(r",\s*true\s*$", M[k + 1:src.pairs.get(k, k)], re.I):
                continue
        t.prints.append((src.line(bs + m.start()), w + ("()" if m.group(1) else "")))
        break
    t.subject = _subject(src, bs, be, _P_KNOWN | {"expect", "toBe", "new", "fn", "function", "array", "sprintf"},
                         re.compile(r"(?:->|::|(?<![\w$>:\\]))\s*([A-Za-z_]\w*)\s*\("))
    t.dup = src.norm(bs, be)


def _php_kind(ctx, m, name):
    src, M = ctx["src"], ctx["src"].m
    if name in ("shouldReceive", "shouldHaveReceived", "shouldNotHaveReceived", "shouldNotReceive", "expects"):
        return "mock"
    k = _ws(M, m.end() - 1)
    if k >= len(M) or M[k] != "(" or k not in src.pairs:
        return "real"
    close = src.pairs[k]
    parts = src.args(k, close)
    if name == "expect":
        chain = src.chain(close + 1, sep="->")
        names = [c[0] for c in chain]
        if "toMatchSnapshot" in names or any(n.startswith("toMatch") and "Snapshot" in n for n in names):
            return "snapshot"
        subj = src.text[k + 1:close]
        if _js_trivial(src, subj, chain):
            return "trivial"
        r = _roots(M[k + 1:close])
        return "mock" if r and r <= {x.replace("this->", "") for x in ctx["mocks"]} else "real"
    if "Snapshot" in name:
        return "snapshot"
    texts = [src.text[a:b].strip() for a, b in parts]
    if name in ("assertTrue", "assertFalse", "assertNull", "assertNotNull") and texts:
        ok, v = _lit(texts[0])
        if ok and {"assertTrue": v is True, "assertFalse": v is False, "assertNull": v is None,
                   "assertNotNull": v is not None}[name]:
            return "trivial"
    if name in ("assertEquals", "assertSame") and len(texts) >= 2 and _lit_or_same(texts[0], texts[1]):
        return "trivial"
    if name in ("assertNotEquals", "assertNotSame") and len(texts) >= 2:
        oka, va = _lit(texts[0])
        okb, vb = _lit(texts[1])
        if oka and okb and va != vb:
            return "trivial"
    if name.startswith("assert"):
        roots = set()
        for a, b in parts:
            for mm in re.finditer(r"\$(this\s*->\s*\w+|\w+)|(?<![\w$>:\\])([a-z_]\w*)\s*\(|\bnew\b", M[a:b]):
                if mm.group(1):
                    roots.add(re.sub(r"\s+", "", mm.group(1)))
                else:
                    roots.add("<call>")
        roots.discard("this")
        if roots and roots <= ctx["mocks"]:
            return "mock"
    return "real"


# --------------------------------------------------------------------------- API

def _analyze(path: Path, rel: str, base: Path) -> list:
    lang = _lang(path)
    if not lang:
        return []
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if len(data) > MAX_BYTES:
        return []
    text = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    try:
        if lang == "py":
            r = _analyze_py(text, path, base, rel)
        elif lang in ("js", "jsx", "ts"):
            r = _analyze_js(text, path, base, rel, lang)
        elif lang in ("java", "kotlin"):
            r = _analyze_jvm(text, path, base, rel, lang)
        elif lang == "go":
            r = _analyze_go(text, path, base, rel)
        else:
            r = _analyze_php(text, path, base, rel)
    except (RecursionError, MemoryError):
        return []
    except Exception:
        if _STRICT:
            raise
        return []  # a file we cannot read must never block a turn
    tests, extra = r[0], r[1]
    bodies = r[2] if len(r) > 2 else {}
    return _judge(rel, lang, tests, extra, bodies)


def _collect(paths, base: Path):
    files, seen = [], set()
    for p in paths:
        pp = Path(p)
        pp = pp if pp.is_absolute() else base / pp
        if pp.is_dir():
            cands = _walk(pp)
        elif pp.is_file() and _lang(pp):
            cands = [pp]
        else:
            cands = []
        for f in cands:
            key = os.path.realpath(f)
            if key not in seen:
                seen.add(key)
                files.append(f)
    return files


def _rel(f: Path, base: Path) -> str:
    try:
        return f.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return f.as_posix()


def _run(paths, top=None):
    base = Path(top) if top else Path.cwd()
    files = _collect(paths, base)
    findings = []
    for f in files:
        findings += _analyze(f, _rel(f, base), base)
    findings.sort(key=lambda x: (x["file"], x["line"], -_RANK[x["severity"]], x["rule"]))
    return findings, len(files)


def audit(paths, top=None) -> list:
    """Findings for the test files under `paths` (files or directories; relative to `top`, else the cwd):
    [{file, line, test, rule, severity, message, fix}], sorted by file and line."""
    return _run(list(paths or []), top)[0]


def _fmt(f) -> str:
    who = f" {f['test']}" if f["test"] else ""
    return f"{f['file']}:{f['line']}{who} — {f['rule']}: {f['message']} → {f['fix']}"


def stop_gate(top, files):
    """For the Stop hook: a short message when the changed test files hold tests that cannot fail, else None."""
    files = [f for f in (files or []) if is_test_file(f)]
    if not files:
        return None
    findings = audit(files, top=top)
    high = [f for f in findings if f["severity"] == "high"]
    if not high:
        return None
    medium = sum(1 for f in findings if f["severity"] == "medium")
    n = len({(f["file"], f["test"]) for f in high})
    lines = [f"devkit test audit: {n} test{'s' if n != 1 else ''} you changed cannot fail for a real bug:"]
    lines += ["  " + _fmt(f) for f in high[:8]]
    if len(high) > 8:
        lines.append(f"  … and {len(high) - 8} more (`dev test audit --changed`)")
    if medium:
        lines.append(f"Also {medium} medium finding{'s' if medium != 1 else ''} (sleeps, skips, duplicates, mocks); "
                     f"run `dev test audit --changed` to see them.")
    lines.append("Make each test able to fail for a real bug, or delete it, before you finish.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- CLI

def add_arguments(p):
    p.add_argument("paths", nargs="*", help="test files or directories (default: the whole repository)")
    p.add_argument("--changed", action="store_true",
                   help="only test files changed on this branch, staged, unstaged or untracked")
    p.add_argument("--since", metavar="REF", help="changed against REF instead of the default branch")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--min-severity", choices=list(SEVERITIES), default="medium",
                   help="hide findings below this severity (default: medium)")
    p.add_argument("--rule", action="append", choices=sorted(RULES), metavar="RULE",
                   help="only this rule (repeatable): " + ", ".join(RULES))


def cmd(a) -> int:
    from . import core, ui
    cwd = Path.cwd()
    top = Path(core.gitout(cwd, "rev-parse", "--show-toplevel") or cwd)
    paths = [str((cwd / p).resolve()) for p in (a.paths or [])]
    if a.changed or a.since:
        changed = [str(top / f) for f in changed_test_files(top, a.since)]
        if paths:
            changed = [f for f in changed if any(f == p or f.startswith(p.rstrip("/") + "/") for p in paths)]
        paths = changed
    elif not paths:
        paths = [str(cwd)]
    findings, nfiles = _run(paths, top) if paths else ([], 0)
    rank = _RANK[a.min_severity]
    sel = [f for f in findings if _RANK[f["severity"]] >= rank and (not a.rule or f["rule"] in a.rule)]
    counts = {s: sum(1 for f in sel if f["severity"] == s) for s in ("high", "medium", "low")}
    rc = 1 if counts["high"] else 0
    if a.json:
        print(json.dumps({"files": nfiles, "counts": counts, "findings": sel}, indent=2, ensure_ascii=False))
        return rc
    print(ui.section(f"Test audit · {nfiles} test file{'s' if nfiles != 1 else ''}"))
    if not nfiles:
        print(f"  {ui.INFO} no test files to audit" + (" (none changed)" if a.changed or a.since else ""))
        return 0
    glyph = {"high": ui.FAIL, "medium": ui.WARN, "low": ui.INFO}
    by_file = {}
    for f in sel:
        by_file.setdefault(f["file"], []).append(f)
    for file, items in by_file.items():
        print(f"\n  {ui.bold(file)}")
        for f in items:
            who = f" {f['test']}" if f["test"] else ""
            print(f"    {glyph[f['severity']]} {str(f['line']).rjust(4)}{who}  {ui.dim(f['rule'])}  {f['message']}")
            print(f"           {ui.ARROW} {ui.dim(f['fix'])}")
    shown = [s for s in ("high", "medium", "low") if _RANK[s] >= rank]
    summary = " · ".join(f"{counts[s]} {s}" for s in shown)
    hidden = sum(1 for f in findings if _RANK[f["severity"]] < rank)
    tail = f"  {ui.dim(f'({hidden} below --min-severity {a.min_severity})')}" if hidden else ""
    mark = ui.FAIL if counts["high"] else ui.WARN if sel else ui.OK
    print(f"\n  {mark} {summary} in {len(by_file)} of {nfiles} test file{'s' if nfiles != 1 else ''}{tail}")
    return rc
