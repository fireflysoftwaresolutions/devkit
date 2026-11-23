# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev test`: the tests that failed last time, then the tests a change can affect, and proof that new tests test it.

  dev test                       1) the last failures (stop if one still fails)  2) the blast radius of the changes;
                                 a branch WIDE_CHANGES files from its base that reaches WIDE_TESTS test files is not
                                 started: it names the tests of the files touched now (--affected runs them all)
  dev test run [paths…]          --failed · --affected · --all · --since REF · --runner NAME · --json; with none of
                                 them, what plain `dev test` runs, started on a wide branch too
  dev test plan [--all]          what would run, why (changed file → tests), the estimate; runs nothing
  dev test prove [tests…]        each new or changed test (each case of a parametrized one) passes N times with the
                                 change and fails without it; tests no runner here can run are listed, not fatal
  dev test audit …               tests that cannot fail (testaudit.py)

Blast radius (`blast_radius`): build, dependency, lock, test-config, shared-fixture and CI files mean "all"; docs mean
"none"; a test file selects itself; a source file selects the tests that depend on it through a static graph
(Python imports parsed with `ast`, JS/TS import/require with relative, index and tsconfig-alias resolution,
Java/Kotlin/PHP class-name references, Go package imports), plus the name convention (foo.py ↔ test_foo.py). A file
nothing maps to widens to the tests of its directory or package, up to "all"; it never selects nothing.

Runners: pytest, unittest, jest, vitest, node (`node --test`, for a package.json test script that runs node, when node
is on the PATH; a plain test script counts as one test, which fails when it exits non-zero; none for a script with a
build step before its tests or a node option devkit does not know written without `=`), maven, gradle, go, phpunit,
pest.

Results: JUnit XML (pytest, node, maven, gradle, phpunit/pest), `go test -json`, jest/vitest JSON and unittest's own
output.
A selection that runs zero tests is a selection problem (exit 2), never a pass.

Recorded per project in STATE/tests/<project>.json: the last failures, durations per test file or class, the last
full-run duration, the tree and the time. Python 3.9 compatible, stdlib only.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import contextlib
import difflib
import fcntl
import fnmatch
import glob
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path

from . import core, ui

TMP_ROOT = None          # parent of `dev test prove` copies; None means core.TMP_ROOT (tests redirect it)
AUDIT_MODULE = "testaudit"
RUNS = 5                 # `dev test prove`: green runs a new test needs with the change present
WIDE_CHANGES = 100       # plain `dev test` does not start the blast radius of a branch with this many changed files…
WIDE_TESTS = 50          # …when it reaches this many test files; it names the tests of the files touched now instead

PY_EXT = {".py"}
JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte"}
JVM_EXT = {".java", ".kt", ".groovy", ".scala"}
LANGS = {**{e: "py" for e in PY_EXT}, **{e: "js" for e in JS_EXT}, **{e: "jvm" for e in JVM_EXT},
         ".go": "go", ".php": "php"}
RUNNER_LANG = {"pytest": "py", "unittest": "py", "jest": "js", "vitest": "js", "node": "js", "playwright": "js",
               "ng": "js", "maven": "jvm", "gradle": "jvm", "go": "go", "phpunit": "php", "pest": "php"}
SKIP_SEGMENTS = {"node_modules", "vendor", ".venv", "venv", "site-packages", ".git"}
DEP_DIRS = ("node_modules", ".venv", "venv", "vendor")


# --------------------------------------------------------------------------- conventions

_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "testdata", "e2e"}


def _split(path: str):
    p = path.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    stem, ext = os.path.splitext(name)
    return p, name, stem, ext.lower()


def _runnable(path: str) -> bool:
    """A file a test runner executes as tests (not a helper or data file next to them)."""
    p, name, stem, ext = _split(path)
    if ext == ".py":
        return name.startswith("test_") or stem.endswith("_test") or name == "tests.py"
    if ext in JS_EXT:
        return bool(re.search(r"\.(test|spec)$", stem)) or "/__tests__/" in "/" + p
    if ext in JVM_EXT:
        return bool(re.search(r"(Test|Tests|IT|ITCase)$", stem) or re.match(r"Test[A-Z]", stem))
    if ext == ".go":
        return stem.endswith("_test")
    if ext == ".php":
        return stem.endswith("Test")
    return False


def is_test_file(path) -> bool:
    """Test code, helpers or data, by the conventions of every stack (test_*.py, *.spec.ts, src/test/java/**, …)."""
    p = str(path).replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return _runnable(p) or _in_test_folder(p)


def _in_test_folder(p: str) -> bool:
    """In a folder of tests (tests/, __tests__/, src/test/, …): test code, helpers or data, whatever the name."""
    return any(seg in _TEST_DIRS for seg in p.split("/")[:-1]) or \
        bool(re.search(r"(^|/)src/[A-Za-z]*[tT]est[A-Za-z]*/", p))


_GLOBAL_NAMES = {
    "pom.xml": "build file", "build.gradle": "build file", "build.gradle.kts": "build file",
    "settings.gradle": "build file", "settings.gradle.kts": "build file", "gradle.properties": "build file",
    "libs.versions.toml": "dependency catalog", "mvnw": "build wrapper", "mvnw.cmd": "build wrapper",
    "gradlew": "build wrapper", "gradlew.bat": "build wrapper", "package.json": "package manifest",
    "package-lock.json": "lockfile", "npm-shrinkwrap.json": "lockfile", "pnpm-lock.yaml": "lockfile",
    "pnpm-workspace.yaml": "workspace file", "yarn.lock": "lockfile", "bun.lockb": "lockfile", "bun.lock": "lockfile",
    ".npmrc": "package manager config", ".yarnrc": "package manager config", ".yarnrc.yml": "package manager config",
    "pyproject.toml": "project file", "setup.py": "project file", "setup.cfg": "project file", "tox.ini": "test config",
    "noxfile.py": "test config", "pytest.ini": "test config", "Pipfile": "dependency file", "Pipfile.lock": "lockfile",
    "poetry.lock": "lockfile", "uv.lock": "lockfile", "go.mod": "module file", "go.sum": "lockfile",
    "go.work": "workspace file", "go.work.sum": "lockfile", "composer.json": "dependency file",
    "composer.lock": "lockfile", "conftest.py": "shared fixtures", "Pest.php": "test config",
    "bootstrap.php": "test bootstrap", "Makefile": "build file", "justfile": "build file", "Taskfile.yml": "build file",
    ".python-version": "toolchain version", ".nvmrc": "toolchain version", ".node-version": "toolchain version",
    ".tool-versions": "toolchain version", ".babelrc": "test config", ".swcrc": "test config",
    "Jenkinsfile": "CI file", ".gitlab-ci.yml": "CI file", "azure-pipelines.yml": "CI file",
    "bitbucket-pipelines.yml": "CI file", ".travis.yml": "CI file",
}
_GLOBAL_GLOBS = (
    ("requirements*.txt", "dependency file"), ("requirements*.in", "dependency file"),
    ("constraints*.txt", "dependency file"), ("jest.config.*", "test config"), ("jest.setup.*", "test setup"),
    ("jest.preset.*", "test config"), ("vitest.config.*", "test config"), ("vitest.setup.*", "test setup"),
    ("vitest.workspace.*", "test config"), ("vite.config.*", "build config"), ("setupTests.*", "test setup"),
    ("setup-tests.*", "test setup"), ("test-setup.*", "test setup"), ("tsconfig*.json", "compiler config"),
    ("jsconfig*.json", "compiler config"), ("phpunit*.xml", "test config"), ("phpunit*.xml.dist", "test config"),
    (".env.test*", "test environment"), ("babel.config.*", "test config"), ("karma.conf.*", "test config"),
    ("docker-compose*.y*ml", "test services"), ("compose*.y*ml", "test services"), ("Dockerfile*", "container build"),
)
_GLOBAL_PREFIX = ((".github/workflows/", "CI file"), (".github/actions/", "CI file"), (".circleci/", "CI file"),
                  (".buildkite/", "CI file"), (".gitlab/", "CI file"))
_SUPPORT_DIRS = {"support", "fixtures", "__fixtures__", "__mocks__", "testutils", "test-utils", "test_utils",
                 "testing", "helpers", "mocks", "factories", "stubs", "testlib", "resources"}


def _global_reason(p: str):
    """Why a change to `p` can affect every test, or None."""
    p, name, _, ext = _split(p)
    segs = p.split("/")
    for prefix, why in _GLOBAL_PREFIX:
        if p.startswith(prefix):
            return why
    if name in _GLOBAL_NAMES:
        return _GLOBAL_NAMES[name]
    for pat, why in _GLOBAL_GLOBS:
        if fnmatch.fnmatchcase(name, pat):
            return why
    if ".mvn" in segs[:-1] or "/gradle/wrapper/" in "/" + p:
        return "build wrapper"
    if "requirements" in segs[:-1] and ext in (".txt", ".in"):
        return "dependency file"
    if is_test_file(p):
        dirs = segs[:-1]
        if name == "__init__.py" and any(s in _TEST_DIRS for s in dirs):
            return "test package"
        if any(s in _SUPPORT_DIRS for s in dirs) or re.search(r"(^|/)src/test[A-Za-z]*/resources/", p) or \
                re.search(r"(^|/)src/testFixtures/", p):
            return "shared test support"
    return None


_DOC_EXT = {".md", ".mdx", ".rst", ".adoc", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
            ".avif", ".pdf", ".drawio", ".excalidraw", ".mp4", ".mov", ".webm"}
_DOC_NAME = re.compile(r"(?i)^(licen[cs]e|notice|changelog|changes|history|authors|contributors|contributing|"
                       r"code_of_conduct|security|codeowners|readme|support|funding)(\..*)?$")
_DOC_PREFIX = ("docs/", "doc/", "documentation/", ".vscode/", ".idea/", ".devcontainer/", ".github/ISSUE_TEMPLATE/")
_DOC_FILES = {".gitignore", ".gitattributes", ".editorconfig", ".mailmap"}
_TOOL_GLOBS = (".eslintrc*", "eslint.config.*", ".eslintignore", ".prettierrc*", "prettier.config.*", ".prettierignore",
               ".stylelintrc*", "stylelint.config.*", "ruff.toml", ".ruff.toml", ".flake8", "mypy.ini", ".mypy.ini",
               ".pylintrc", "pylintrc", ".markdownlint*", ".vale.ini", ".golangci.y*ml", "phpstan*.neon*",
               "psalm.xml*", ".php-cs-fixer*", "pint.json", "checkstyle*.xml", "spotbugs*.xml", "pmd*.xml",
               ".pre-commit-config.y*ml", ".gitleaks.toml", ".gitleaksignore", ".commitlintrc*",
               "commitlint.config.*", ".lintstagedrc*", "renovate.json*", ".releaserc*", "release-please-config.json",
               ".release-please-manifest.json", "cliff.toml", ".yamllint*", ".hadolint.y*ml", ".shellcheckrc",
               ".spectral.y*ml", "biome.json*", ".browserslistrc", ".dockerignore", "dependabot.y*ml", ".coveragerc")
_TOOL_PREFIX = (".husky/", ".vale/", ".github/CODEOWNERS", ".github/FUNDING", ".github/labeler", ".github/release")


def _is_doc(p: str) -> bool:
    p, name, _, ext = _split(p)
    return ext in _DOC_EXT or bool(_DOC_NAME.match(name)) or name in _DOC_FILES or p.startswith(_DOC_PREFIX)


def _conventional_doc(p: str) -> bool:
    """Docs nobody's code reads: docs/, README, LICENSE, CHANGELOG, …"""
    p, name, _, _ = _split(p)
    return p.startswith(_DOC_PREFIX) or bool(_DOC_NAME.match(name)) or name in _DOC_FILES


def _tool_config(p: str) -> bool:
    p, name, _, _ = _split(p)
    return p.startswith(_TOOL_PREFIX) or any(fnmatch.fnmatchcase(name, g) for g in _TOOL_GLOBS)


def _lang(p: str):
    return LANGS.get(_split(p)[3])


# --------------------------------------------------------------------------- the repository, read once per call


def _git(top, *args, timeout=60) -> str:
    rc, out, _ = core.git(top, *args, timeout=timeout)
    return out if rc == 0 else ""


def _ls(top, *flags) -> list:
    out = _git(top, "ls-files", "-z", *flags)
    return [f for f in out.split("\0") if f and not (SKIP_SEGMENTS & set(f.split("/")[:-1]))]


_NAME_TOKEN = re.compile(r"(?<![\w.-])([\w-]+(?:\.[\w-]+)+)(?![\w-])")


class _Repo:
    """The files of a work tree (tracked and untracked, not ignored) with lazily read contents."""

    def __init__(self, top, extra=()):
        self.top = str(top)
        listed = _ls(self.top, "-co", "--exclude-standard")
        deleted = set(_ls(self.top, "-d"))
        self.existing = [f for f in dict.fromkeys(listed) if f not in deleted]
        self.ex = set(self.existing)
        self.all = self.ex | set(extra)
        self._text, self._graphs = {}, {}
        self._runners = None
        self._pytest = {}  # folder -> the pytest collection rules that govern it (_pytest_rules)
        self.listed = {}  # file -> whether its runner's listing holds it (dev test --listed): the runner's word
        self.tests = sorted(f for f in self.existing if self.runnable(f))
        if listing_on():
            self._apply_listing()

    def _apply_listing(self):
        """Which files hold tests, as each runner that can list says (list_tests): its listing replaces the file-name
        rules for the files of its language under its root, except in a folder it lists whole (a package that fails
        to import), where the file rules decide."""
        for r in self.runners():
            files = list_tests(self.top, r, self)
            if files is None:
                continue
            folders = [x for x in files if x.endswith("/")]  # their files are tests by the file rules
            for f in self.existing:
                if _lang(f) == r["lang"] and _under(f, r["root"]) and owner(self.runners(), f) is r:
                    self.listed[f] = f in files or (any(f.startswith(d) for d in folders) and self.runnable(f))
            r["selection"] = "listed"
        self.tests = sorted(f for f in self.existing if self.runnable(f))

    def runnable(self, f) -> bool:
        """A file a runner here executes as tests: named as one (_runnable) and, for Python, one pytest's own
        configuration collects. pytest collects any file named on its command line, so devkit applies its testpaths
        and python_files itself: a production module named test_… outside them is no test. With the runner's
        listing (dev test --listed), the listing decides."""
        if f in self.listed:
            return self.listed[f]
        if not _runnable(f):
            return False
        if not f.endswith(".py"):
            return True
        return _pytest_collects(self._pytest_rules(f.rpartition("/")[0]), f)

    def is_test(self, f) -> bool:
        """Test code, helpers or data (is_test_file), except a module named as a test that no runner here collects
        outside a folder of tests: that is production code."""
        return (self.runnable(f) or _in_test_folder(f)) if _runnable(f) else is_test_file(f)

    def _pytest_rules(self, d):
        """The pytest configuration nearest to folder `d`, from `d` up to the top (_pytest_config)."""
        if d not in self._pytest:
            here = _pytest_config(self.top, d)
            self._pytest[d] = here if here or not d else self._pytest_rules(d.rpartition("/")[0])
        return self._pytest[d]

    def text(self, rel) -> str:
        if rel not in self._text:
            try:
                with open(os.path.join(self.top, rel), "rb") as fh:
                    data = fh.read(2_000_000)
                self._text[rel] = data.decode("utf-8", "replace") if b"\0" not in data[:4096] else ""
            except OSError:
                self._text[rel] = ""
        return self._text[rel]

    def by_lang(self, lang) -> list:
        return [f for f in self.existing if _lang(f) == lang]

    def names(self) -> dict:
        """File-name-like tokens (body.md, skill-rules.json) -> code files that contain them; built once."""
        if "names" not in self._graphs:
            idx = {}
            for f in self.existing:
                if _lang(f):
                    for tok in set(_NAME_TOKEN.findall(self.text(f))):
                        idx.setdefault(tok, set()).add(f)
            self._graphs["names"] = idx
        return self._graphs["names"]

    def runners(self) -> list:
        if self._runners is None:
            self._runners = detect(self.top, self)
        return self._runners

    def graph(self, lang):
        if lang not in self._graphs:
            self._graphs[lang] = {"py": _py_graph, "js": _js_graph, "go": _go_graph}[lang](self)
        return self._graphs[lang]


# --------------------------------------------------------------------------- the runner's own listing

LISTING_SECONDS = 300  # a listing that takes longer is no listing: the file rules decide
_LIST_CONFIGS = ("pytest.ini", ".pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py", "package.json",
                 "angular.json") + tuple(f"{t}.config.{e}" for t in ("vitest", "vite", "jest", "playwright")
                                         for e in ("ts", "js", "mjs", "cjs", "mts", "cts"))


def listing_on() -> bool:
    """dev test --listed, or config test.listing: the runners' own listings say which files hold tests."""
    return os.environ.get("DEVKIT_TEST_LISTING") == "1" or bool((core.CFG.get("test") or {}).get("listing"))


def _list_key(top, runner, repo, argv=()) -> str:
    """What a runner's listing depends on: the runner and its command, the listing command devkit runs (its script:
    a devkit that lists otherwise lists again) and devkit's version, its configuration files (their text, conftest
    files too) and the names of the files of its language under its root (a new test file lists again)."""
    h = hashlib.sha256(json.dumps([runner["name"], runner["root"], runner.get("exec"), [str(x) for x in argv],
                                   core.VERSION]).encode())
    for f in repo.existing:
        if _under(f, runner["root"]) and (f.rpartition("/")[2] in _LIST_CONFIGS or _lang(f) == runner["lang"]):
            h.update(f.encode() + b"\0")
            if f.rpartition("/")[2] in _LIST_CONFIGS:
                h.update(repo.text(f).encode("utf-8", "replace") + b"\0")
    return h.hexdigest()


# a module that fails to import is listed as unittest.loader's stand-in test named after the module: it is a test file
# that fails, so it is listed by the file its name finds; a package that fails to import hides its modules, so it is
# listed as its folder (a trailing separator), whose files the file rules decide
_LIST_UNITTEST = """import importlib.util, json, os, sys, unittest
start, top = sys.argv[1], sys.argv[2]
suite = unittest.TestLoader().discover(start, top_level_dir=top)
out = set()
def walk(s):
    for x in s:
        if isinstance(x, unittest.TestSuite):
            walk(x)
        elif type(x).__module__ != "unittest.loader":
            out.add(sys.modules[type(x).__module__].__file__)
        else:
            try:
                spec = importlib.util.find_spec(x._testMethodName)
            except Exception:
                spec = None
            if spec is not None and spec.submodule_search_locations:
                out.update(os.path.join(p, "") for p in spec.submodule_search_locations)
            elif spec is not None and spec.origin:
                out.add(spec.origin)
walk(suite)
print(json.dumps(sorted(out)))
"""


def _list_command(top, runner) -> tuple:
    """(argv, folder, parse(output, folder) -> absolute paths) of a runner's listing, or None."""
    name, ex, root = runner["name"], list(runner.get("exec") or []), _rootdir(top, runner)

    def lines(text, rx):
        return [m.group(1) for m in re.finditer(rx, text)]

    def json_files(text):
        found = []

        def walk(x):
            if isinstance(x, dict):
                if isinstance(x.get("file"), str):
                    found.append(x["file"])
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        try:
            walk(json.loads(text[text.find("[") if text.lstrip().startswith("[") else text.find("{"):]))
        except ValueError:
            return None
        return found
    if name == "pytest":
        return ex + ["--collect-only", "-q", "-p", "no:cacheprovider"], root, \
            lambda out, d: [os.path.join(d, x) for x in dict.fromkeys(lines(out, r"(?m)^([^\s:]+\.py)::"))]
    if name == "unittest":
        start = runner.get("start") or "."
        level = "." if runner.get("package") else start
        return [runner.get("python") or sys.executable, "-c", _LIST_UNITTEST, start, level], root, \
            lambda out, d: json.loads(out.strip().splitlines()[-1]) if out.strip() else None
    if name == "vitest":
        return ex + ["list", "--filesOnly", "--json"], root, \
            lambda out, d: [os.path.join(d, x) for x in json_files(out) or []] if json_files(out) is not None else None
    if name == "playwright":
        return ex + ["--list", "--reporter=json"], root, _playwright_files
    if name == "jest":
        return ex + ["--listTests"], root, lambda out, d: [x.strip() for x in out.splitlines() if x.strip().startswith("/")]
    return None


def _playwright_files(out, d):
    """The spec files of `playwright test --list --reporter=json`: each suite's file, relative to config.rootDir."""
    try:
        data = json.loads(out[out.find("{"):])
    except ValueError:
        return None
    base = ((data.get("config") or {}).get("rootDir") if isinstance(data, dict) else None) or d
    return [os.path.join(base, s["file"]) for s in data.get("suites") or [] if isinstance(s, dict) and s.get("file")]


def list_tests(top, runner, repo=None):
    """The files (relative to the top) that `runner`'s own listing says hold tests, or None when it cannot list them
    (no listing for the runner, or the listing failed). Cached in devkit's state by _list_key."""
    top = str(top)
    repo = repo or _Repo(top)
    cmd = _list_command(top, runner)
    if cmd is None:
        return None
    key = _list_key(top, runner, repo, cmd[0])
    cache = core.STATE / "test-lists" / f"{project_key(top)[1]}--{hashlib.sha256((runner['root'] + runner['name']).encode()).hexdigest()[:12]}.json"
    held = core.read_json(cache, {}) or {}
    if held.get("key") == key and isinstance(held.get("files"), list):
        return set(held["files"])
    from . import identity
    argv, folder, parse = cmd
    env = dict(identity.runner_env(dict(os.environ)), PYTHONDONTWRITEBYTECODE="1", CI="1")
    t0 = time.time()
    try:
        p = subprocess.run([str(x) for x in argv], cwd=folder, env=env, capture_output=True, text=True,
                           errors="replace", timeout=LISTING_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        found = parse(p.stdout, folder) if p.returncode in (0, 5) else None
    except (ValueError, IndexError, KeyError):
        found = None
    if found is None:
        return None
    real_top = os.path.realpath(top)
    # a folder (a trailing separator) keeps its trailing slash: the file rules decide its files
    files = sorted({os.path.relpath(os.path.realpath(x), real_top).replace(os.sep, "/") + "/" * x.endswith(os.sep)
                    for x in found if os.path.realpath(x).startswith(real_top + os.sep)})
    cache.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{cache.stem}-", suffix=".tmp", dir=str(cache.parent))
    try:  # written whole, then moved in place: a reader never sees half of it
        with os.fdopen(fd, "w") as fh:
            json.dump({"key": key, "files": files, "secs": round(time.time() - t0, 2), "at": time.time()}, fh)
        os.replace(tmp, cache)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return set(files)


# --------------------------------------------------------------------------- changed files


_STACK_REFS = ("refs/heads/release", "refs/heads/integration", "refs/remotes/origin/release",
               "refs/remotes/origin/integration")


def base_ref(top, since=None):
    """-> (commit sha or None, label): the merge-base with origin/<default>, or `since`. A branch cut from a release or
    integration branch that the default branch does not have yet starts where it left that branch (_stack_base)."""
    if since:
        sha = _git(top, "rev-parse", "--verify", "-q", f"{since}^{{commit}}")
        return (sha or None), since
    refs = []
    head = _git(top, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    refs += [head] if head else []
    refs += ["origin/main", "origin/master"]
    current = _git(top, "branch", "--show-current")
    refs += [b for b in ("main", "master") if b != current]
    for ref in refs:
        if _git(top, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"):
            mb = _git(top, "merge-base", "HEAD", ref)
            if mb:
                return _stack_base(top, ref, mb, current) or (mb, ref)
    return None, "HEAD"


def _stack_base(top, default, mb, current):
    """The release or integration branch, here or on origin, that this branch was cut from while `default` does not
    have it yet -> (its merge-base with HEAD, its name), the nearest one when there are several; None when there is
    none. Against `default`, such a branch's change would hold the whole release: the copy without the change would
    lose the release's code, and the release's own tests would count as this branch's. The branch itself and its copy
    on origin are left out, and so is one that went on past HEAD: it holds this branch (merged it), not the other way
    round. One that points at HEAD is the branch this one was just cut from, before its first commit."""
    own = {f"refs/heads/{current}", f"refs/remotes/origin/{current}"} if current else set()
    upstream = _git(top, "rev-parse", "-q", "--symbolic-full-name", "@{upstream}") if current else ""
    own |= {upstream} if upstream else set()
    names = _git(top, "for-each-ref", f"--no-merged={default}", "--format=%(refname)", *_STACK_REFS).split()
    tip = _git(top, "rev-parse", "HEAD")
    best = None
    for ref in [n for n in names if n not in own]:
        fork = _git(top, "merge-base", "HEAD", ref)
        if not fork or fork == mb or (fork == tip and _git(top, "rev-parse", ref) != tip):
            continue
        if core.git(top, "merge-base", "--is-ancestor", mb, fork)[0] != 0:
            continue
        ahead = int(_git(top, "rev-list", "--count", f"{fork}..HEAD") or 0)
        if best is None or ahead < best[0]:
            best = (ahead, fork, ref.split("/", 2)[2])
    return (best[1], best[2]) if best else None


def changed_files(top, since=None) -> list:
    """Files changed against the base (base_ref: the merge-base with origin/<default>, the release or integration
    branch this one was cut from, or `since`), plus staged, unstaged and untracked
    (not ignored) files; deleted files included. Paths relative to the repository top, sorted."""
    top = str(top)
    base, _ = base_ref(top, since)
    out = set()
    if base:
        out.update(_git(top, "diff", "--name-only", "--no-renames", "-z", base).split("\0"))
    elif _git(top, "rev-parse", "--verify", "-q", "HEAD"):
        out.update(_git(top, "diff", "--name-only", "--no-renames", "-z", "HEAD").split("\0"))
    else:
        out.update(_git(top, "diff", "--name-only", "--no-renames", "-z", "--cached").split("\0"))
    out.update(_ls(top, "-o", "--exclude-standard"))
    return sorted(f for f in out if f)


def _lane_layers(top):
    """The scope of the crew lane that runs this command -> (layers of globs, the folder they count from), as
    `dev commit` finds and counts it (gitx._commit_lane): DEVKIT_FROM_AGENT or the member the call ledger names, its
    globs and those of the open steps it owns, inside its parents', counted from the checkout the scope was given in.
    ([], "") in the main session, a developer's shell, or a lane without a scope."""
    from types import SimpleNamespace
    from . import gitx, lanescope
    lane = gitx._commit_lane(SimpleNamespace(top=str(top)))
    if not lane or not lane[1]:
        return [], ""
    _, layers, base = lane
    # the globs name files of the run's repository: in another one (dev test -p, a suite's temporary repositories)
    # they say nothing about what changed. Another checkout of the same repository is the lane's, and this one's
    # changes are not
    if os.path.realpath(base) != os.path.realpath(str(top)) and \
            lanescope._common_dir(base) != lanescope._common_dir(str(top)):
        return [], ""
    return layers, base


def lane_files(top, files) -> tuple:
    """`files` (relative to `top`) split by the scope of the crew lane that runs this command -> (inside, outside).
    In a shared checkout the other lanes' changes are theirs to test: a member's unscoped `dev test` and
    `dev test prove` take the changed files of its own scope. Outside a lane, or when the lane cannot be read, all
    files are inside."""
    from . import lanescope
    try:
        layers, base = _lane_layers(top)
    except Exception as e:  # an unreadable run must not stop the tests: they run as outside a lane
        print(ui.dim(f"could not read this crew lane's scope ({type(e).__name__}: {e}): taking every changed file"),
              file=sys.stderr)
        return list(files), []
    if not layers:
        return list(files), []
    inside = [f for f in files if all(lanescope.scope_holds(f, top, layer, base=base) for layer in layers)]
    return inside, [f for f in files if f not in set(inside)]


def _outside_note(outside) -> None:
    if outside:
        print(ui.dim(f"left out {len(outside)} changed file(s) outside this crew lane's scope, another lane's work "
                     f"({', '.join(outside[:3])}{' …' if len(outside) > 3 else ''}): dev test run <test files> runs "
                     "them"), file=sys.stderr)


# --------------------------------------------------------------------------- runner detection


def _read(path) -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def _pm_exec(top, root, tool) -> list:
    d = Path(top) / root
    stop = Path(top).resolve()
    pkg = core.read_json(d / "package.json", {}) or {}
    pm_field = str(pkg.get("packageManager") or "")
    while True:
        if (d / "pnpm-lock.yaml").exists() or pm_field.startswith("pnpm"):
            return ["pnpm", "exec", tool]
        if (d / "yarn.lock").exists() or pm_field.startswith("yarn"):
            return ["yarn", tool]
        if (d / "bun.lockb").exists() or (d / "bun.lock").exists() or pm_field.startswith("bun"):
            return ["bunx", tool]
        if (d / "package-lock.json").exists():
            break
        if d.resolve() == stop or d.parent == d:
            break
        d = d.parent
    return ["npx", "--no-install", tool]


def _py_env(top, root):
    """The Python environment of the package at `root`: the nearest folder from it up to the repository's top with a
    .venv or a uv.lock (a uv workspace keeps one lock at its root for all its members) -> (that .venv's python, False),
    (None, True) for a uv.lock without a .venv beside it, or (None, False) when neither is there."""
    stop, d = Path(top).resolve(), (Path(top) / root).resolve()
    while True:
        py = d / ".venv" / "bin" / "python"
        if py.exists():
            return str(Path(top) / os.path.relpath(py, stop)), False
        if (d / "uv.lock").exists():
            return None, True
        if d == stop or stop not in d.parents:
            return None, False
        d = d.parent


# node's options that take a value, written `--opt value` or `--opt=value`, and its switches, which take none (any
# `--no-…` is a switch too). An option in neither set, written without `=`, may own the next word: see _node_args.
_NODE_VALUE = {"--import", "--require", "-r", "--loader", "--experimental-loader", "--conditions", "-C", "--env-file",
               "--env-file-if-exists", "--input-type", "--experimental-default-type", "--disable-warning",
               "--unhandled-rejections", "--title", "--max-old-space-size", "--stack-size", "--test-name-pattern",
               "--test-skip-pattern", "--test-concurrency", "--test-timeout", "--test-shard", "--test-reporter",
               "--test-reporter-destination", "--test-coverage-include", "--test-coverage-exclude",
               "--test-coverage-lines", "--test-coverage-branches", "--test-coverage-functions", "--test-isolation",
               "--experimental-test-isolation", "--test-global-setup", "--watch-path", "--watch-kill-signal"}
_NODE_SWITCH = {"--test", "--test-only", "--test-force-exit", "--test-update-snapshots", "--experimental-test-coverage",
                "--experimental-test-module-mocks", "--experimental-test-snapshots", "--watch",
                "--watch-preserve-output", "--enable-source-maps", "--experimental-strip-types",
                "--experimental-transform-types", "--experimental-vm-modules", "--experimental-require-module",
                "--experimental-detect-module", "--experimental-sqlite", "--experimental-websocket",
                "--experimental-global-navigator", "--preserve-symlinks", "--preserve-symlinks-main", "--expose-gc",
                "--trace-warnings", "--trace-deprecation", "--trace-uncaught", "--throw-deprecation",
                "--pending-deprecation", "--abort-on-uncaught-exception", "--inspect", "--inspect-brk"}


def _node_args(args):
    """node's arguments -> ([(option, its words)], the words after the options), or None when an option devkit does
    not know is written without `=`: its value may be the next word, so what the call runs is unknown. Options end at
    the first other word (`--` ends them too): after it, `node --test` takes paths and globs, and a script its own
    arguments. node reads `_` in an option's name as `-`."""
    opts, i = [], 0
    while i < len(args):
        x = args[i]
        if x == "--":
            i += 1
            break
        if x == "-" or not x.startswith("-"):
            break
        name = x.split("=", 1)[0]
        name = name[:2] + name[2:].replace("_", "-")
        if name in _NODE_VALUE and "=" not in x:
            opts.append((name, args[i:i + 2]))
            i += 2
            continue
        if "=" not in x and name not in _NODE_SWITCH and not name.startswith("--no-"):
            return None
        opts.append((name, [x]))
        i += 1
    return opts, args[i:]


# A pipe or a redirect outside quotes and escapes (group 1): `node --test | tee out.txt`, `node --test > out.txt`
_SHELL_IO = re.compile(r"""'[^']*'|"(?:\\.|[^"\\])*"|\\.|([|<>])""")


def _node_suite(script: str):
    """A package test script that runs node -> (node's own flags, what it runs), or None. `node --test [flags]
    [paths or globs]` gives its flags and paths ([] for node's own discovery); plain `node <test script>` calls joined
    by `&&` give their scripts, which `node --test` runs as one test each (a script fails when it exits non-zero). The
    flags are those of the first call that runs tests, `node --test` or a plain script (`node --import tsx a.test.ts`).

    None when devkit cannot run what the script runs: a step before the tests (a build such as `tsc`, or a node
    script that is no test) would be skipped and leave the tests to run on stale output, a node option devkit does
    not know, written without `=`, leaves the paths unknown, and a pipe or a redirect on the tests (`| tee out.txt`,
    `> out.txt`, `< input`) sends their output elsewhere or feeds them input. A step after the tests changes nothing
    they run."""
    flags, suite, tested, runs, before = [], [], False, False, False
    for part in re.split(r"&&|\|\||;", script or ""):
        try:
            words = shlex.split(part)
        except ValueError:
            return None
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):  # FOO=1 node …
            words.pop(0)
        if not words:
            continue
        node = os.path.basename(words[0]) in ("node", "node.exe")
        parsed = _node_args(words[1:]) if node else ([], [])
        if parsed is None:
            return None
        opts, rest = parsed
        test = any(n == "--test" for n, _ in opts)
        script_test = node and not test and bool(rest) and _lang(rest[0]) == "js" and is_test_file(rest[0])
        if not (test or script_test):  # another command, or a script that is no test (a build, another framework)
            before = True
            continue
        if before or any(m.group(1) for m in _SHELL_IO.finditer(part)):
            return None
        runs = True
        # a test script of its own (the words after it are its arguments), or node --test's paths and globs
        suite += [rest[0]] if script_test else [x for x in rest if not x.startswith("-")]
        # the flags of the first node call that runs tests (a loader, an env file); never a watch or a reporter
        if not tested:
            tested = True
            flags += [w for n, ws in opts if n != "--test" and not n.startswith(("--test-reporter", "--watch"))
                      for w in ws]
    return (flags, suite) if runs else None


def _outermost(dirs) -> list:
    """Directories not nested inside another one of the set ('' is the top)."""
    out = []
    for d in sorted(dirs, key=lambda x: (x.count("/"), x)):
        if not any(o == "" or d == o or d.startswith(o + "/") for o in out):
            out.append(d)
    return out


def detect(top, repo=None) -> list:
    """Test runners of the repository and its subprojects: [{name, root ('.' = top), lang, exec, …}]."""
    top = str(top)
    repo = repo or _Repo(top)
    by_dir = {}
    for f in repo.existing:
        d, _, b = f.rpartition("/")
        if not ({"testdata", "fixtures", "__fixtures__"} | _SUPPORT_DIRS) & set(d.split("/")):  # sample projects
            by_dir.setdefault(d, set()).add(b)
    has = lambda d, *names: any(n in by_dir.get(d, ()) for n in names)  # noqa: E731
    rel = lambda d: d or "."  # noqa: E731
    absd = lambda d: os.path.join(top, d) if d else top  # noqa: E731
    found = []

    # Python
    py_tests = [f for f in repo.tests if f.endswith(".py")]
    roots = [d for d in by_dir if has(d, "pyproject.toml", "setup.cfg", "setup.py", "pytest.ini", "tox.ini")]
    if py_tests and not any(f.startswith(r + "/") or r == "" for f in py_tests for r in roots):
        roots.append("")
    for d in sorted(set(roots)):
        mine = [f for f in py_tests if d == "" or f.startswith(d + "/")]
        texts = " ".join(_read(os.path.join(absd(d), n)) for n in ("pyproject.toml", "setup.cfg", "tox.ini"))
        texts += " ".join(_read(os.path.join(top, f)) for f in repo.existing
                          if f.rpartition("/")[0] == d and fnmatch.fnmatchcase(f.rpartition("/")[2], "requirements*.txt"))
        conftest = any(f.endswith("conftest.py") and (d == "" or f.startswith(d + "/")) for f in repo.existing)
        pytest = has(d, "pytest.ini") or "[tool.pytest" in texts or "[tool:pytest]" in texts or "[pytest]" in texts \
            or conftest or re.search(r"(?m)^\s*[\"']?pytest\b", texts) is not None
        if not (pytest or mine):
            continue
        venv, uv = _py_env(top, d)
        py = venv or sys.executable
        if pytest:
            ex = [py, "-m", "pytest"]
            if uv and shutil.which("uv"):
                ex = ["uv", "run", "pytest"]
            found.append({"name": "pytest", "root": rel(d), "lang": "py", "exec": ex, "python": py})
        else:
            start = next((s for s in ("tests", "test") if any(f.startswith((d + "/" if d else "") + s + "/")
                                                             for f in mine)), ".")
            found.append({"name": "unittest", "root": rel(d), "lang": "py", "exec": [py, "-m", "unittest"],
                          "python": py, "start": start,
                          "package": start != "." and os.path.isfile(os.path.join(absd(d), start, "__init__.py"))})

    # A Python project nested in a pytest runner's folder has a runner of its own: pytest run there for the whole suite
    # (no file arguments) would walk into it and collect its tests in the outer process
    py_roots = [x["root"] for x in found if x["lang"] == "py"]
    for x in found:
        if x["name"] == "pytest":
            x["nested"] = sorted(r if x["root"] == "." else r[len(x["root"]) + 1:] for r in py_roots
                                 if r != x["root"] and _under(r, x["root"]))

    # Node: jest or vitest per package.json
    for d in sorted(d for d in by_dir if has(d, "package.json")):
        pkg = core.read_json(os.path.join(absd(d), "package.json"), {}) or {}
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        scripts = pkg.get("scripts") or {}
        script = str(scripts.get("test") or "")
        names = by_dir.get(d, set())
        tool = None
        if re.search(r"\bvitest\b", script):
            tool = "vitest"
        elif re.search(r"\bjest\b", script):
            tool = "jest"
        elif "vitest" in deps or any(n.startswith("vitest.") for n in names):
            tool = "vitest"
        elif "jest" in deps or any(n.startswith("jest.config") for n in names) or "jest" in pkg:
            tool = "jest"
        # npm runs a pretest script (a build) before the test script; devkit runs node itself and would skip it
        node = None if tool or scripts.get("pretest") or not shutil.which("node") else _node_suite(script)
        if tool:
            found.append({"name": tool, "root": rel(d), "lang": "js", "exec": _pm_exec(top, d, tool)})
        elif node:
            found.append({"name": "node", "root": rel(d), "lang": "js", "exec": ["node"] + node[0] + ["--test"],
                          "suite": node[1]})

    # Playwright: the specs of a playwright.config's testDir folders run with Playwright, not with the package's unit
    # runner, which excludes them. A config without testDir finds specs from its own folder: there, only the specs that
    # import @playwright/test are Playwright's.
    for d in sorted(d for d in by_dir if any(n in by_dir[d] for n in _PW_CONFIGS)):
        text = _js_code(_read(os.path.join(absd(d), next(n for n in _PW_CONFIGS if n in by_dir[d]))))
        named = ["/".join(re.findall(r"""['"`]([^'"`]+)['"`]""", m.group(1))) for m in _PW_TESTDIR.finditer(text)]
        # a testDir held in a variable names no folder; one that is the config's own folder searches as none does
        dirs = sorted({_folder(d, x) for x in named if x} - {None, d})
        pw = {"name": "playwright", "root": rel(d), "lang": "js", "exec": _pm_exec(top, d, "playwright") + ["test"],
              "claims": dirs}
        if not dirs:
            pw["specs"] = sorted(f for f in repo.tests if _lang(f) == "js" and _under(f, rel(d)) and
                                 _uses_playwright(repo, f))
        found.append(pw)

    # Angular: a project whose test target is the unit-test builder with its Vitest runner runs through `ng test`, which
    # builds the specs with the workspace's TypeScript paths, setup files and providers (bare vitest loses them). It
    # runs the specs under its source root and those its include option names elsewhere.
    for d in sorted(d for d in by_dir if has(d, "angular.json")):
        ws = _json_loose(_read(os.path.join(absd(d), "angular.json")))
        projects = ws.get("projects") if isinstance(ws, dict) else None
        for name, proj in sorted(projects.items() if isinstance(projects, dict) else ()):
            targets = (proj.get("architect") or proj.get("targets")) if isinstance(proj, dict) else None
            target = targets.get("test") if isinstance(targets, dict) else None
            opts = target.get("options") if isinstance(target, dict) else None
            opts = opts if isinstance(opts, dict) else {}
            if not isinstance(target, dict) or target.get("builder") != "@angular/build:unit-test" or \
                    (opts.get("runner") or "vitest") != "vitest":
                continue
            src = str(proj.get("sourceRoot") or posixpath.join(str(proj.get("root") or ""), "src"))
            include = [p for p in opts.get("include") or ("**/*.spec.ts", "**/*.test.ts") if isinstance(p, str)]
            claims = sorted({_folder(d, posixpath.join(src, p)) for p in include} - {None})  # file patterns
            found.append({"name": "ng", "root": rel(d), "lang": "js", "project": name, "claims": claims,
                          "exec": _pm_exec(top, d, "ng") + ["test", name]})

    # Maven and Gradle: the outermost build of each tree
    for d in _outermost([d for d in by_dir if has(d, "pom.xml")]):
        found.append({"name": "maven", "root": rel(d), "lang": "jvm",
                      "exec": ["./mvnw"] if has(d, "mvnw") else ["mvn"]})
    for d in _outermost([d for d in by_dir if has(d, "build.gradle", "build.gradle.kts", "settings.gradle",
                                                    "settings.gradle.kts")]):
        found.append({"name": "gradle", "root": rel(d), "lang": "jvm",
                      "exec": ["./gradlew"] if has(d, "gradlew") else ["gradle"]})

    # Go modules
    for d in sorted(d for d in by_dir if has(d, "go.mod")):
        m = re.search(r"(?m)^\s*module\s+(\S+)", _read(os.path.join(absd(d), "go.mod")))
        found.append({"name": "go", "root": rel(d), "lang": "go", "exec": ["go", "test"],
                      "module": m.group(1) if m else ""})

    # PHP
    for d in sorted(d for d in by_dir if has(d, "composer.json")):
        comp = core.read_json(os.path.join(absd(d), "composer.json"), {}) or {}
        deps = {**(comp.get("require") or {}), **(comp.get("require-dev") or {})}
        pest = "pestphp/pest" in deps or (d + "/tests/Pest.php" if d else "tests/Pest.php") in repo.ex
        if pest:
            found.append({"name": "pest", "root": rel(d), "lang": "php", "exec": ["vendor/bin/pest"]})
        elif "phpunit/phpunit" in deps or has(d, "phpunit.xml", "phpunit.xml.dist", "phpunit.dist.xml"):
            found.append({"name": "phpunit", "root": rel(d), "lang": "php", "exec": ["vendor/bin/phpunit"]})
    return _declare(top, found, by_dir)


def declared(top, by_dir=None) -> list:
    """The test commands the project declares, in the order they decide: [{command, cwd, source}] from the test_gate
    of its delivery policy (dev delivery), then the run steps of its CI workflows (as dev ci reads them), then each
    package.json's test script."""
    top = str(top)
    out = []
    try:
        from . import delivery
        policy = delivery.load(delivery.scope_for(top)) or {}
    except Exception:  # noqa: BLE001 - a project outside the registry or a broken policy declares nothing
        policy = {}
    for cmd in policy.get("test_gate") or []:
        if isinstance(cmd, str):
            out.append({"command": cmd, "cwd": top, "source": "delivery policy test_gate"})
    try:
        from . import gitx
        jobs = gitx.plan(top) if os.path.isdir(os.path.join(top, ".github", "workflows")) else []
    except Exception:  # noqa: BLE001 - a workflow dev ci cannot read declares nothing
        jobs = []
    for j in jobs:
        for st in j.get("steps") or []:
            if st.get("run"):
                cwd = st.get("cwd") or "."
                out.append({"command": st["run"], "cwd": cwd if os.path.isabs(cwd) else os.path.join(top, cwd),
                            "source": f"CI .github/workflows/{j.get('file')} › {j.get('id') or j.get('job')} › "
                                      f"{st.get('name') or st['run'].splitlines()[0][:40]}"})
    for d in sorted(d for d, names in (by_dir or {}).items() if "package.json" in names):
        pkg = core.read_json(os.path.join(top, d, "package.json"), {}) or {}
        if str((pkg.get("scripts") or {}).get("test") or "").strip():
            out.append({"command": "npm test", "cwd": os.path.join(top, d) if d else top,
                        "source": f"{d + '/' if d else ''}package.json test script"})
    return out


_BARE_PYTHON = re.compile(r"python[\d.]*|py")


def _declare(top, found, by_dir=None) -> list:
    """Each runner found takes the command the project declares for it (declared: the first that is a plain call of
    the same runner at the same root, testx.recognize), and says where it came from: source "declared" with
    declared_by, or "inferred". A bare `python` of a CI step is the interpreter devkit found here, as the CI's
    setup-python is there."""
    decls = []
    for decl in declared(top, by_dir):
        r = recognize(decl["command"], decl["cwd"], top)
        if r:
            decls.append((r, decl))
    for x in found:
        x["source"], x["declared_by"] = "inferred", ""
        for r, decl in decls:
            if r["name"] != x["name"] or r["root"] != x["root"]:
                continue
            ex = list(r["exec"])
            if ex and _BARE_PYTHON.fullmatch(os.path.basename(ex[0])) and x["exec"][:2] == ["uv", "run"]:
                # a uv project without a .venv: its interpreter is uv's, not the one devkit runs on
                ex[:1] = ["uv", "run", "python"]
            elif ex and _BARE_PYTHON.fullmatch(os.path.basename(ex[0])) and x.get("python"):
                ex[0] = x["python"]
            elif ex and ex[0] in ("pytest", "py.test") and x["name"] == "pytest":
                # a bare pytest is the CI environment's: here that is the pytest devkit runs for this project (its
                # .venv's interpreter, or uv run in a uv project), not PATH's pytest
                ex[:1] = list(x["exec"])
            if x["lang"] == "py" or r["name"] in ("go", "maven", "gradle"):
                x["exec"] = ex
            for k in ("start", "package", "scope", "env", "argv"):
                if r.get(k) not in (None, [], {}):
                    x[k] = r[k]
            x.update(source="declared", declared_by=decl["source"], declared=decl["command"])
            break
    return found


def _under(path: str, root: str) -> bool:
    return root in (".", "") or path == root or path.startswith(root.rstrip("/") + "/")


def _claim(r, path):
    """How closely runner `r` claims test file `path` -> (depth, specific) or None. A runner runs the tests under its
    root, as deep as that root; a runner that names its own folders (Playwright's testDir, an Angular project's
    source root) runs only the tests under them, as deep as the folder, and wins a tie over its package's runner. A
    claim with a wildcard (an Angular include pattern) holds the files it matches, as deep as its literal folder."""
    if not _under(path, r["root"]):
        return None
    if "claims" not in r:
        return len(r["root"]) if r["root"] != "." else 0, 0
    hits = [len(c) for c in r["claims"] if not _GLOB_HEAD.search(c) and _under(path, c)]
    hits += [len(_glob_dir(c)) for c in r["claims"] if _GLOB_HEAD.search(c) and _glob_re(c).fullmatch(path)]
    hits += [len(path)] if path in r.get("specs", ()) else []
    return (max(hits), 1) if hits else None


def owner(runners, path):
    """The runner that runs test file `path`: same language, the one that claims it most closely (_claim)."""
    lang = _lang(path)
    cands = [(c, r) for r in runners if r["lang"] == lang for c in [_claim(r, path)] if c is not None]
    return max(cands, key=lambda x: x[0])[1] if cands else None


_PW_CONFIGS = tuple(f"playwright.config.{e}" for e in ("ts", "js", "mjs", "cjs", "mts", "cts"))
_PW_TESTDIR = re.compile(r"""\btestDir\s*:\s*([^,\n}]*)""")
_GLOB_HEAD = re.compile(r"[*?\[{]")


def _glob_dir(g: str) -> str:
    """The literal folder a file pattern starts with ('' when its first segment has a wildcard)."""
    return _GLOB_HEAD.split(g)[0].rpartition("/")[0]


def _glob_re(g: str):
    """A file pattern as tools such as Angular's include read it -> a regex for paths: `*` and `?` stay inside one
    segment, `**` spans any number of them (none too), `{a,b}` is either, `[…]` one character of a set."""
    out, i, n = [], 0, len(g)
    while i < n:
        ch = g[i]
        if g.startswith("**/", i):
            out.append("(?:[^/]*/)*")
            i += 3
            continue
        if g.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "{" and "}" in g[i:]:
            j = g.index("}", i)
            out.append("(?:" + "|".join(re.escape(x) for x in g[i + 1:j].split(",")) + ")")
            i = j + 1
            continue
        elif ch == "[" and "]" in g[i + 1:]:
            j = g.index("]", i + 1)
            body = g[i + 1:j]
            out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
            i = j + 1
            continue
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out))


def _js_code(text: str) -> str:
    """JavaScript or TypeScript source without its comments (line and block), strings kept: a commented-out setting
    or import is no setting. A comment's line breaks stay, so line numbers and line-anchored patterns hold."""
    out, i, n, quote = [], 0, len(text), None
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == quote or (ch == "\n" and quote != "`"):
                quote = None
        elif ch in "'\"`":
            quote = ch
            out.append(ch)
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            end = n if j < 0 else j + 2
            out.append("\n" * text.count("\n", i, end) or " ")
            i = end
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


_JS_TYPE_ONLY = re.compile(r"""^(?:import|export)\s+type\b""")
_JS_ONLY_NAMED = re.compile(r"""^(?:import|export)\s*\{([^}]*)\}\s*from\b""")  # no default or namespace import


def _js_value_specs(code: str) -> list:
    """The modules JS/TS `code` loads at run time: every import, export … from, require and dynamic import, except
    those that bring in types only (import type, export type … from, import { type A, type B } from), which the
    compiler erases."""
    out = []
    for m in _JS_SPEC.finditer(code):
        stmt = m.group(0)
        if _JS_TYPE_ONLY.match(stmt):
            continue
        named = _JS_ONLY_NAMED.match(stmt)
        names = [x.split() for x in named.group(1).split(",") if x.strip()] if named else []
        if names and all(x[0] == "type" for x in names):
            continue  # every name in the braces is a type (`import {} from` still loads the module)
        out.append(m.group(1))
    return out


def _uses_playwright(repo, f, seen=None) -> bool:
    """Does JavaScript file `f` load @playwright/test at run time, itself or through the local modules it loads (a
    fixtures file that extends Playwright's test)? A type imported from it, directly or by a helper, loads nothing."""
    seen = set() if seen is None else seen
    if f in seen:
        return False
    seen.add(f)
    specs = _js_value_specs(_js_code(repo.text(f)))
    if any(s == "@playwright/test" or s.startswith("@playwright/test/") for s in specs):
        return True
    d = f.rpartition("/")[0]
    return any(_uses_playwright(repo, t, seen) for s in specs if s.startswith(".")
               for t in _js_candidates(repo, posixpath.join(d, s)))


def _folder(base, path):
    """`path`, relative to folder `base` of the top, as a folder relative to the top ('' for the top itself; None
    outside the top or for an absolute path)."""
    if path.startswith("/"):
        return None
    p = posixpath.normpath(posixpath.join(base or ".", path))
    return None if p == ".." or p.startswith("../") else "" if p == "." else p


# --------------------------------------------------------------------------- Python import graph

_PY_DYN = re.compile(r"""(?:import_module|__import__)\(\s*['"]([\w.]+)['"]""")


_PY_STMT = re.compile(r"(?:^|[;:]\s*)((?:from\s+[.\w]+\s+)?import\s.*)$")


def _py_stmts(text: str) -> list:
    """Import statements, line by line (only lines that mention import; parenthesised and continued ones joined)."""
    out, lines, i = [], text.split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        i += 1
        if "import" not in ln:
            continue
        m = _PY_STMT.search(ln.strip())
        if not m:
            continue
        stmt = m.group(1)
        if "(" in stmt and ")" not in stmt:
            while i < len(lines) and ")" not in lines[i - 1]:
                stmt += " " + lines[i].strip()
                i += 1
        while stmt.endswith("\\") and i < len(lines):
            stmt = stmt[:-1] + " " + lines[i].strip()
            i += 1
        out.append(stmt)
    if "import_module" in text or "__import__" in text:
        out += [f"import {m.group(1)}" for m in _PY_DYN.finditer(text)]
    return out


def _py_nodes(text: str) -> list:
    """Import statements of a Python file: found line by line, then parsed with ast (fast on big trees)."""
    if "import" not in text:
        return []
    stmts = _py_stmts(text)
    nodes = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            nodes = list(ast.parse("\n".join(stmts)).body)
        except (SyntaxError, ValueError):
            for st in stmts:
                try:
                    nodes += ast.parse(st).body
                except (SyntaxError, ValueError):
                    continue
    return [n for n in nodes if isinstance(n, (ast.Import, ast.ImportFrom))]


def _py_rooted(f: str, files) -> str:
    """The name Python imports `f` by from its root: lib/devkit/core.py -> devkit.core (lib/ is not a package)."""
    dirs = f.split("/")[:-1]
    name = [] if f.endswith("/__init__.py") or f == "__init__.py" else [f.rsplit("/", 1)[-1][:-3]]
    i = len(dirs)
    while i > 0 and "/".join(dirs[:i]) + "/__init__.py" in files:
        i -= 1
    return ".".join(dirs[i:] + name)


def _py_graph(repo) -> dict:
    files = [f for f in repo.all if f.endswith(".py")]
    index, rooted = {}, {}
    for f in files:
        parts = f[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        for i in range(len(parts)):
            index.setdefault(".".join(parts[i:]), set()).add(f)
        rooted[f] = _py_rooted(f, repo.all)
    rev = {}
    for f in files:
        if f not in repo.ex:
            continue
        for node in _py_nodes(repo.text(f)):
            for t in _py_targets(repo, f, node, index, rooted):
                if t != f:
                    rev.setdefault(t, set()).add(f)
    return rev


def _py_targets(repo, f, node, index, rooted=None) -> set:
    out = set()
    if isinstance(node, ast.ImportFrom) and node.level:
        base = f.split("/")[:-1]
        base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
        mod = (node.module or "").split(".") if node.module else []
        stem = "/".join(base + mod)
        cands = [stem + ".py", (stem + "/" if stem else "") + "__init__.py"]
        for a in node.names:
            sub = (stem + "/" if stem else "") + a.name
            cands += [sub + ".py", sub + "/__init__.py"]
        return {c for c in cands if c in repo.all}
    if isinstance(node, ast.Import):
        mods = [a.name for a in node.names]
    else:
        mods = [f"{node.module}.{a.name}" for a in node.names] + [node.module or ""]
    here = f.rpartition("/")[0]
    for m in mods:
        parts = [p for p in m.split(".") if p]
        for i in range(len(parts), 0, -1):
            name = ".".join(parts[:i])
            cands = index.get(name)
            if not cands or name == "__main__":
                continue
            exact = {c for c in cands if (rooted or {}).get(c) == name}
            if exact:
                out |= exact
            elif i >= 2:  # a dotted name matched by its tail: namespace packages, sys.path tricks
                out |= cands
            else:  # a bare name: only a sibling module (a script importing its neighbour)
                out |= {c for c in cands if c.rpartition("/")[0] == here}
    return out


# --------------------------------------------------------------------------- JS/TS import graph

_JS_SPEC = re.compile(r"""(?:\bimport\s*(?:[^'"`;()]*?\bfrom\s*)?|\bexport\s+[^'"`;()]*?\bfrom\s*|\brequire\s*\(\s*"""
                      r"""|\bimport\s*\(\s*|\b(?:jest|vi)\.(?:mock|doMock|unmock|requireActual|importActual)\s*\(\s*)"""
                      r"""['"]([^'"\n]+)['"]""")
_JS_TRY = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte", ".json")


def _json_loose(text: str):
    """JSON with // and /* */ comments and trailing commas (tsconfig)."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(ch)
        i += 1
    try:
        return json.loads(re.sub(r",\s*([}\]])", r"\1", "".join(out)))
    except ValueError:
        return None


def _js_aliases(repo) -> list:
    """[(dir, prefix, [target dirs])] from tsconfig/jsconfig paths and baseUrl, plus the @/ and ~/ → src/ habit."""
    out = []
    for f in repo.existing:
        name = f.rpartition("/")[2]
        if not re.fullmatch(r"(tsconfig|jsconfig)[\w.-]*\.json", name):
            continue
        d = f.rpartition("/")[0]
        data = _json_loose(repo.text(f)) or {}
        co = data.get("compilerOptions") or {}
        base = os.path.normpath(os.path.join(d, co.get("baseUrl") or ".")).replace("\\", "/")
        base = "" if base == "." else base
        for key, targets in (co.get("paths") or {}).items():
            pre = key[:-1] if key.endswith("*") else key
            tdirs = [os.path.normpath(os.path.join(base, str(t).rstrip("*"))).replace("\\", "/") +
                     ("/" if str(t).endswith("/*") else "") for t in targets or []]
            out.append((d, pre, tdirs))
        if co.get("baseUrl"):
            out.append((d, "", [base + "/" if base else ""]))
    for r in repo.runners():
        if r["lang"] == "js":
            d = "" if r["root"] == "." else r["root"]
            src = (d + "/" if d else "") + "src/"
            out += [(d, "@/", [src]), (d, "~/", [src])]
    return out


def _js_candidates(repo, base: str) -> list:
    base = os.path.normpath(base).replace("\\", "/")
    out = [base] if base in repo.all else []
    stem, ext = os.path.splitext(base)
    if ext in (".js", ".jsx", ".mjs", ".cjs"):
        out += [stem + e for e in (".ts", ".tsx", ".mts", ".cts") if stem + e in repo.all]
    out += [base + e for e in _JS_TRY if base + e in repo.all]
    out += [base + "/index" + e for e in _JS_TRY if base + "/index" + e in repo.all]
    return out


def _js_graph(repo) -> dict:
    aliases = _js_aliases(repo)
    pkgs = {}
    for f in repo.existing:
        if f.rpartition("/")[2] == "package.json":
            name = (core.read_json(os.path.join(repo.top, f), {}) or {}).get("name")
            if name:
                pkgs[name] = f.rpartition("/")[0]
    rev = {}
    for f in repo.existing:
        if _lang(f) != "js":
            continue
        d = f.rpartition("/")[0]
        for spec in _JS_SPEC.findall(repo.text(f)):
            if spec.startswith("."):
                targets = _js_candidates(repo, os.path.join(d, spec))
            else:
                targets = []
                for adir, pre, tdirs in aliases:
                    if (adir == "" or f.startswith(adir + "/")) and pre and spec.startswith(pre):
                        for t in tdirs:
                            targets += _js_candidates(repo, t + spec[len(pre):])
                    elif (adir == "" or f.startswith(adir + "/")) and not pre:
                        targets += _js_candidates(repo, tdirs[0] + spec)
                pkg = next((n for n in pkgs if spec == n or spec.startswith(n + "/")), None)
                if pkg is not None:
                    targets.append("@pkg:" + pkgs[pkg])
            for t in targets:
                if t != f:
                    rev.setdefault(t, set()).add(f)
    return rev


# --------------------------------------------------------------------------- Go package graph

_GO_IMPORT = re.compile(r'(?m)^\s*import\s*(?:\((.*?)\)|((?:[\w.]+\s+)?"[^"]+"))', re.S)


def _go_graph(repo) -> dict:
    mods = {}
    for f in repo.existing:
        if f.rpartition("/")[2] == "go.mod":
            m = re.search(r"(?m)^\s*module\s+(\S+)", repo.text(f))
            if m:
                mods[m.group(1)] = f.rpartition("/")[0]
    rev = {}
    for f in repo.existing:
        if not f.endswith(".go"):
            continue
        d = f.rpartition("/")[0]
        for block, single in _GO_IMPORT.findall(repo.text(f)):
            for ip in re.findall(r'"([^"]+)"', block or single):
                mod = max((m for m in mods if ip == m or ip.startswith(m + "/")), key=len, default=None)
                if mod is None:
                    continue
                sub = ip[len(mod):].lstrip("/")
                target = "/".join(x for x in (mods[mod], sub) if x)
                if target != d:
                    rev.setdefault(target, set()).add(d)
    return rev


# --------------------------------------------------------------------------- dependents


def _closure(rev: dict, starts, extra=None) -> set:
    seen, todo = set(starts), list(starts)
    while todo:
        x = todo.pop()
        nxt = set(rev.get(x, ()))
        if extra:
            nxt |= extra(x)
        for y in nxt - seen:
            seen.add(y)
            todo.append(y)
    return seen


def _name_closure(repo, starts, lang) -> set:
    """Java/Kotlin/PHP: files that mention a changed class by its simple name, transitively."""
    files = repo.by_lang(lang)
    seen = set(starts)
    frontier = {_split(s)[2] for s in starts}
    used = set()
    while frontier:
        used |= frontier
        rx = re.compile(r"\b(?:" + "|".join(map(re.escape, sorted(frontier))) + r")\b")
        new = [f for f in files if f not in seen and rx.search(repo.text(f))]
        seen.update(new)
        frontier = {_split(f)[2] for f in new} - used
    return seen


def _dependents(repo, starts) -> set:
    """Every file that depends on one of `starts` through code the graphs can see (starts included)."""
    out = set(starts)
    by = {}
    for s in starts:
        by.setdefault(_lang(s), []).append(s)
    if by.get("py"):
        out |= _closure(repo.graph("py"), by["py"])
    if by.get("js"):
        rev = repo.graph("js")
        pkg_dirs = sorted({k[5:] for k in rev if k.startswith("@pkg:")})

        def via_pkg(x):
            return set().union(*[rev.get("@pkg:" + d, set()) for d in pkg_dirs if d == "" or x.startswith(d + "/")])

        out |= {x for x in _closure(rev, by["js"], via_pkg if pkg_dirs else None) if not x.startswith("@pkg:")}
    for lang in ("jvm", "php"):
        if by.get(lang):
            out |= _name_closure(repo, by[lang], lang)
    if by.get("go"):
        dirs = _closure(repo.graph("go"), {s.rpartition("/")[0] for s in by["go"]})
        out |= {f for f in repo.existing if f.endswith(".go") and f.rpartition("/")[0] in dirs}
    return out


_GENERIC = {"__init__.py", "__main__.py", "main.go", "doc.go", "index.js", "index.ts", "index.tsx", "index.jsx",
            "index.mjs", "index.vue", "mod.rs", "main.py", "app.py"}


def _distinctive(name: str) -> bool:
    stem, ext = os.path.splitext(name)
    return name not in _GENERIC and (len(name) >= 5 if ext else len(name) >= 4)


def _named_in(repo, name, among) -> set:
    """Files of `among` that contain `name` as a word-like token (an index once many names are asked)."""
    repo.lookups = getattr(repo, "lookups", 0) + 1
    if "." in name and (repo.lookups > 25 or "names" in repo._graphs):
        return set(repo.names().get(name, ())) & set(among)
    rx = re.compile(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])")
    return {f for f in among if name in repo.text(f) and rx.search(repo.text(f))}


def _referencing(repo, p) -> list:
    """Code files that name `p` (its basename) as a string: loaders, templates, fixtures."""
    name = p.rpartition("/")[2]
    if not _distinctive(name):
        return []
    out = _named_in(repo, name, [f for f in repo.existing if _lang(f)]) - {p}
    if (_lang(p) is None or _lang(p) == "js") and repo.by_lang("js"):
        out |= set(repo.graph("js").get(p, ()))
    return sorted(out)


def _convention(repo, p) -> set:
    _, name, stem, ext = _split(p)
    lang = _lang(p)
    names = set()
    if lang == "py":
        names = {f"test_{stem}.py", f"{stem}_test.py"}
    elif lang == "js":
        names = {f"{stem}.{k}{e}" for k in ("test", "spec") for e in JS_EXT}
    elif lang == "jvm":
        names = {f"{stem}{k}{e}" for k in ("Test", "Tests", "IT", "ITCase") for e in JVM_EXT} | \
                {f"Test{stem}{e}" for e in JVM_EXT}
    elif lang == "go":
        names = {f"{stem}_test.go"}
    elif lang == "php":
        names = {f"{stem}Test.php"}
    out = {t for t in repo.tests if t.rpartition("/")[2] in names}
    if lang == "js":
        out |= {t for t in repo.tests if "/__tests__/" in "/" + t and _split(t)[2] == stem}
    return out


def _mentions(repo, p) -> set:
    name = p.rpartition("/")[2]
    if not _distinctive(name):
        return set()
    return _named_in(repo, name, repo.tests) - {p}


def _root_of(repo, p) -> str:
    """The deepest runner root containing `p` ('.' when none or the top)."""
    roots = [r["root"] for r in repo.runners() if r["root"] != "." and _under(p, r["root"])]
    return max(roots, key=len) if roots else "."


def _mirrors(rel_dir: str) -> list:
    segs = [s for s in rel_dir.split("/") if s]
    out = []
    if "main" in segs and "src" in segs:
        i = segs.index("main")
        out.append("/".join(segs[:i] + ["test"] + segs[i + 1:]))
    for t in ("tests", "test", "spec", "__tests__"):
        out.append("/".join([t] + segs))
        if len(segs) > 1 and segs[0] in ("src", "lib", "app", "pkg", "internal"):
            out.append("/".join([t] + segs[1:]))
    return out


def _widen(repo, p, why):
    """No test maps to `p`: the tests that walk a folder holding it, else the tests of its directory or package, then
    its parents', up to its runner (all)."""
    walked = _walked_by(repo, p)
    if walked:
        return "some", walked, f"{why}: {len(walked)} test file(s) read the folder it is in", None
    root = _root_of(repo, p)
    rootd = "" if root == "." else root
    d = p.rpartition("/")[0]
    while d != rootd and (rootd == "" or d.startswith(rootd + "/")) and d:
        rel = d[len(rootd) + 1:] if rootd else d
        dirs = [d] + [(rootd + "/" if rootd else "") + m for m in _mirrors(rel)]
        sel = {t for t in repo.tests if any(t.startswith(x + "/") for x in dirs)}
        if sel:
            return "some", sel, f"{why}: widened to the tests under {d}/", None
        d = d.rpartition("/")[0]
    where = f"the tests of {root}/" if rootd else "all tests"
    return "all", set(), f"{why}: widened to {where}", root


# A test that walks a folder (Path.rglob/glob/iterdir/walk, os.walk/listdir/scandir, glob.glob) reads every file under
# it without importing one. The folder is the quoted path the test names on the line of the walk, written whole
# (os.walk('lib/devkit')) or joined with / ((ROOT / "lib" / "devkit").rglob("*.py")).
_TREE_WALK = re.compile(r"\.(?:r?glob|iterdir|walk)\(|\bos\.(?:walk|listdir|scandir)\(|\bglob\.i?glob\(")
_QUOTED_CHAIN = re.compile(r"""(?:(['"])[\w.-]+(?:/[\w.-]+)*\1\s*/\s*)*(['"])[\w.-]+(?:/[\w.-]+)*\2""")
_QUOTED = re.compile(r"""(['"])([\w./-]+)\1""")


def _tree_readers(repo) -> dict:
    """Test file -> the folders of this repository it walks (see _TREE_WALK); built once per repository."""
    if "trees" not in repo._graphs:
        dirs = set()
        for f in repo.existing:
            d = f.rpartition("/")[0]
            while d and d not in dirs:
                dirs.add(d)
                d = d.rpartition("/")[0]
        out = {}
        for t in repo.tests:
            text = repo.text(t)
            lines = [ln for ln in text.splitlines() if _TREE_WALK.search(ln)] if _TREE_WALK.search(text) else []
            if not lines:
                continue
            root = _root_of(repo, t)
            found = set()
            for m in _QUOTED_CHAIN.finditer("\n".join(lines)):  # the folder named on the walk's own line
                d = "/".join(q[1].strip("/") for q in _QUOTED.findall(m.group(0))).strip("/")
                for c in ((d, f"{root}/{d}") if root != "." else (d,)):
                    if c in dirs:
                        found.add(c)
            if found:
                out[t] = found
        repo._graphs["trees"] = out
    return repo._graphs["trees"]


def _walked_by(repo, p) -> set:
    """The tests that walk a folder containing `p` (_tree_readers)."""
    return {t for t, ds in _tree_readers(repo).items() if t != p and any(p.startswith(d + "/") for d in ds)}


def _quoted_by(repo, p) -> set:
    """The tests of the code files that name `p` by its whole path in quotes: a script that reads that file at run
    time (scripts/security_corpus.py quotes reviewed lines of lib/devkit/state.py), and tests that quote it."""
    quoters = [f for f in repo.existing if f != p and _lang(f) and (f"'{p}'" in repo.text(f) or f'"{p}"' in repo.text(f))]
    out = {t for t in _dependents(repo, quoters) if t in repo.tests}
    for f in quoters:
        out |= _mentions(repo, f)
    return out - {p}


def _one(repo, p):
    """-> (mode, tests, why, scope) for one changed file, with the tests that walk a folder holding it."""
    mode, sel, why, scope = _one_file(repo, p)
    if mode == "all":
        return mode, sel, why, scope
    walked = _walked_by(repo, p) - sel
    if not walked:
        return mode, sel, why, scope
    note = f"{len(walked)} test file(s) read the folder it is in"
    if mode == "some" and sel:
        return "some", sel | walked, f"{why}; {note}", scope
    return "some", walked, note, None  # what nothing else selected, or a doc's "no tests", is these tests


def _one_file(repo, p):
    """-> (mode, tests, why, scope) for one changed file."""
    g = _global_reason(p)
    d = p.rpartition("/")[0]
    if g and p.endswith(".py") and p.rpartition("/")[2] != "conftest.py" and \
            (d + "/__init__.py" if d else "__init__.py") in repo.all:
        g = None  # setup.py, noxfile.py inside a package: a module like any other
    if g:
        return "all", set(), f"{g}: every test can depend on it", _root_of(repo, p)
    if repo.is_test(p):
        if repo.runnable(p):
            return ("some", {p}, "test file", None) if p in repo.ex else ("some", set(), "deleted test file", None)
        if p in repo.ex and _node_script(repo, repo.runners(), p):  # a plain script node --test runs as a test
            return "some", {p}, "test script", None
        if _lang(p):
            sel = {t for t in _dependents(repo, [p]) if t in repo.tests}
            return ("some", sel, f"test helper used by {len(sel)} test file(s)", None) if sel else \
                _widen(repo, p, "test helper")
        refs = _referencing(repo, p)
        sel = {t for t in _dependents(repo, refs) if t in repo.tests} if refs else set()
        return ("some", sel, f"test data read by {refs[0]}", None) if sel else _widen(repo, p, "test data")
    if _tool_config(p):
        return "some", set(), "tool configuration (lint, format, release): runs no tests", None
    if _is_doc(p):
        refs = [] if _conventional_doc(p) else _referencing(repo, p)
        sel = {t for t in _dependents(repo, refs) if t in repo.tests} if refs else set()
        if sel:
            return "some", sel, f"read by {refs[0]}", None
        return "none", set(), "docs: no tests", None
    if _lang(p):
        sel = {t for t in _dependents(repo, [p]) if t in repo.tests} | _convention(repo, p) | _mentions(repo, p) | \
            _quoted_by(repo, p)
        sel.discard(p)
        if sel:
            return "some", sel, f"{len(sel)} test file(s) depend on it", None
        return _widen(repo, p, "no test imports or names it")
    refs = _referencing(repo, p)
    sel = {t for t in _dependents(repo, refs) if t in repo.tests} if refs else set()
    if sel:
        return "some", sel, f"read by {refs[0]}", None
    return _widen(repo, p, "nothing references it by name")


def blast_radius(top, changed) -> dict:
    """-> {"mode": "none"|"some"|"all", "tests": [...], "reasons": [[file, why], ...], "all_roots": [...]}.

    "all" when a global file changed (the reasons name it first; `all_roots` says which runner roots, '.' = every
    runner); "none" when only docs changed; otherwise the test files the changes can affect. Never narrows on doubt."""
    top = str(top)
    changed = sorted({(c[2:] if c.startswith("./") else c).replace("\\", "/") for c in changed or [] if c})
    if not changed:
        return {"mode": "none", "tests": [], "reasons": [], "all_roots": [], "by_file": {}}
    repo = _Repo(top, extra=changed)
    tests, firsts, rest, roots = set(), [], [], []
    modes, by_file = set(), {}
    for p in changed:
        mode, sel, why, scope = _one(repo, p)
        modes.add(mode)
        tests |= sel
        by_file[p] = sorted(sel)
        (firsts if mode == "all" else rest).append([p, why])
        if mode == "all" and scope not in roots:
            roots.append(scope)
    mode = "all" if "all" in modes else "some" if "some" in modes else "none"
    if "." in roots:
        roots = ["."]
    return {"mode": mode, "tests": sorted(tests), "reasons": firsts + rest, "all_roots": roots, "by_file": by_file}


# --------------------------------------------------------------------------- commands


def _toml_strings(text, sections, key):
    """The string or array of strings of `key` in the first of TOML `sections` of `text` that has it, or None. Enough
    TOML for pytest's testpaths and python_files on every Python devkit runs on (tomllib needs 3.11)."""
    for section in sections:
        m = re.search(r"(?m)^\[" + re.escape(section) + r"\][ \t]*(?:#.*)?$", text)
        if not m:
            continue
        body = re.split(r"(?m)^\[", text[m.end():], maxsplit=1)[0]
        v = re.search(r"(?m)^[ \t]*" + re.escape(key) + r"""[ \t]*=[ \t]*(\[(?:[^]"'#]|"[^"\n]*"|'[^'\n]*'|#[^\n]*)*\]"""
                      r"""|"[^"\n]*"|'[^'\n]*')""", body)
        if v:
            return [a or b for a, b, comment in re.findall(r"""("[^"\n]*")|('[^'\n]*')|(#[^\n]*)""", v.group(1))
                    if not comment for a, b in [(a[1:-1], b[1:-1])]]
    return None


def _ini_strings(text, section, key):
    """The whitespace-separated values of `key` in INI `section` of `text` (pytest.ini, tox.ini, setup.cfg), or
    None."""
    import configparser
    cp = configparser.RawConfigParser(strict=False)
    try:
        cp.read_string(text)
    except configparser.Error:
        return None
    return cp.get(section, key).split() if cp.has_option(section, key) else None


def _pytest_config(top, d):
    """The pytest configuration in folder `d` (relative to `top`), from the first file pytest reads there ->
    (d, [testpaths that exist] or None, [python_files] or None), or None when `d` has none. pytest collects from its
    folder when no testpath exists, so testpaths that match nothing count as none."""
    here = os.path.join(top, d) if d else top
    for name, section in _PYTEST_CONFIGS:
        path = os.path.join(here, name)
        text = _read(path)
        if not (os.path.isfile(path) and (not section or section in text)):  # pytest.ini counts even when empty
            continue
        if name == "pyproject.toml":
            pick = lambda k: _toml_strings(text, ("tool.pytest.ini_options", "tool.pytest"), k)  # noqa: E731
        else:
            ini = "tool:pytest" if name == "setup.cfg" else "pytest"
            pick = lambda k: _ini_strings(text, ini, k)  # noqa: E731
        testpaths = [re.sub(r"^(?:\./)+", "", t.replace("\\", "/")).strip("/") for t in pick("testpaths") or ()]
        if "" in testpaths or "." in testpaths or not any(glob.glob(os.path.join(here, t)) for t in testpaths):
            testpaths = None
        return d, testpaths, pick("python_files")
    return None


def _pytest_collects(rules, f) -> bool:
    """Whether pytest, run with no file arguments, collects test file `f` (relative to the top) under `rules`
    (_pytest_config): inside one of its testpaths (shell patterns allowed), named as one of its python_files."""
    if not rules:
        return True
    base, testpaths, python_files = rules
    rel = f[len(base) + 1:] if base else f
    name = rel.rpartition("/")[2]
    if python_files and not any(fnmatch.fnmatch(name, pat) if "/" not in pat else
                                fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, "*/" + pat) for pat in python_files):
        return False
    if testpaths:
        parts = rel.split("/")
        heads = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]  # app, app/x, app/x/test_y.py
        return any(fnmatch.fnmatch(h, t) for t in testpaths for h in heads)
    return True


def _rootdir(top, runner) -> str:
    return top if runner["root"] in (".", "") else os.path.join(top, runner["root"])


def _rel(top, runner, f) -> str:
    return os.path.relpath(os.path.join(top, f), _rootdir(top, runner)).replace("\\", "/")


def _id(x):
    return x["id"] if isinstance(x, dict) else str(x)


def _jvm_class(relpath: str) -> str:
    """src/test/java/com/acme/FooTest.java → com.acme.FooTest."""
    p = relpath.replace("\\", "/")
    m = re.search(r"(?:^|/)src/[^/]+/(?:java|kotlin|groovy|scala)/(.+)\.\w+$", p)
    return (m.group(1) if m else os.path.splitext(p)[0].rsplit("/", 1)[-1]).replace("/", ".")


def _gradle_project(top, runner, f) -> str:
    """The Gradle project path (:api, '' for the root) that owns file `f` (relative to the top)."""
    root = _rootdir(top, runner)
    d = os.path.dirname(os.path.join(top, f))
    while os.path.abspath(d).startswith(os.path.abspath(root)) and os.path.abspath(d) != os.path.abspath(root):
        if any(os.path.exists(os.path.join(d, b)) for b in ("build.gradle", "build.gradle.kts")):
            return ":" + os.path.relpath(d, root).replace(os.sep, ":")
        d = os.path.dirname(d)
    return ""


def build_command(top, runner, files=(), ids=(), report=None, related=(), all_=False) -> dict:
    """The runner command for a selection: test `files` (relative to the top), runner `ids` (or failure dicts),
    `related` source files (jest --findRelatedTests / vitest related) or everything (`all_`).
    -> {"argv", "cwd", "env", "report"}."""
    top = str(top)
    name, ex = runner["name"], list(runner["exec"])
    files, ids, related = list(files), list(ids), list(related)
    rel = [_rel(top, runner, f) for f in files]
    env, names, only = {}, [], ""
    if name == "pytest":  # the whole suite leaves out the nested Python projects, which their own runners run
        argv = ex + ["-q"] + ([f"--junitxml={report}"] if report else []) + \
            ([f"--ignore={n}" for n in runner.get("nested", ())] if all_ else rel + [_id(i) for i in ids])
    elif name == "unittest":
        start = runner.get("start") or "."
        sdir = os.path.join(_rootdir(top, runner), start)
        env["PYTHONPATH"] = os.pathsep.join([os.path.normpath(sdir)] + [p for p in [os.environ.get("PYTHONPATH")] if p])
        if all_:
            argv = ex + ["discover", "-v", "-s", start] + (["-t", "."] if runner.get("package") else [])
        else:
            names = [_ut_module(runner, r) for r in rel] + [_ut_name(runner, _id(i)) for i in ids]
            argv = ex + ["-v"] + names
    elif name == "jest":
        argv = ex + ["--ci", "--json"] + ([f"--outputFile={report}"] if report else [])
        if related:
            argv += ["--findRelatedTests"] + [_rel(top, runner, f) for f in related]
        elif not all_:
            argv += rel + sorted({_rel(top, runner, _id(i).split("::")[0]) for i in ids})
    elif name == "vitest":
        out = ["--reporter=default", "--reporter=json"] + ([f"--outputFile.json={report}"] if report else [])
        if related:
            argv = ex + ["related", "--run"] + out + [_rel(top, runner, f) for f in related]
        else:
            argv = ex + ["run"] + out + ([] if all_ else rel + sorted({_rel(top, runner, _id(i).split("::")[0])
                                                                        for i in ids}))
    elif name == "node":  # readable output on stdout, the JUnit report in a file; the whole suite is the script's
        out = ["--test-reporter=spec", "--test-reporter-destination=stdout", "--test-reporter=junit",
               f"--test-reporter-destination={report}"] if report else []
        picked = [] if all_ else rel + sorted({_rel(top, runner, _id(i).split("::")[0]) for i in ids} - set(rel))
        argv = ex + out + (list(runner.get("suite") or []) if all_ else picked)
        if len(picked) == 1:  # Node before 24 names no file in its JUnit cases: the one file run is theirs
            only = os.path.join(_rootdir(top, runner), picked[0])
    elif name == "playwright":  # file arguments filter the specs; the JUnit report goes to the file its variable names
        if report:
            env["PLAYWRIGHT_JUNIT_OUTPUT_FILE"] = env["PLAYWRIGHT_JUNIT_OUTPUT_NAME"] = report
        argv = ex + ["--reporter=list,junit" if report else "--reporter=list"]
        if not all_:
            argv += rel + sorted({_rel(top, runner, _id(i).split("::")[0]) for i in ids} - set(rel))
            titles = [_id(i).split("::", 1)[1].rsplit(" › ", 1)[-1] for i in ids if "::" in _id(i)]
            if titles and not rel:  # a recorded failure reruns by its title
                argv += ["--grep", "|".join(_js_regex(t) for t in dict.fromkeys(titles))]
    elif name == "ng":  # the Angular unit-test builder, its Vitest runner's JSON report in a file
        env["NG_CLI_ANALYTICS"] = "false"
        argv = ex + ["--watch=false"] + (["--reporters=default", "--reporters=json", f"--output-file={report}"]
                                         if report else [])
        if not all_:
            for f in rel + sorted({_rel(top, runner, _id(i).split("::")[0]) for i in ids} - set(rel)):
                argv += ["--include", f]
            full = [_id(i).split("::", 1)[1] for i in ids if "::" in _id(i)]
            if full and not rel:  # a recorded failure reruns by its full name
                argv += ["--filter", "|".join(_js_regex(n) for n in dict.fromkeys(full))]
    elif name == "maven":
        if runner.get("argv"):  # a CI step's own command: keep its goals and flags, add the selection
            ex = list(runner["argv"])
        if all_:
            argv = ex if runner.get("argv") else ex + ["-B", "verify"]
        else:
            names = [os.path.splitext(r.rsplit("/", 1)[-1])[0] for r in rel] + [_id(i) for i in ids]
            names = list(dict.fromkeys(names))
            its = [n for n in names if re.search(r"(IT|ITCase)(#|$)", n)]
            units = [n for n in names if n not in its]
            goal = [] if runner.get("argv") else ["-B", "verify"]
            if its:
                argv = ex + goal + ["-Dtest=" + (",".join(units) or "NoUnitTestsSelected_devkit"),
                             "-Dit.test=" + ",".join(its), "-Dsurefire.failIfNoSpecifiedTests=false",
                             "-Dfailsafe.failIfNoSpecifiedTests=false"]
            else:
                goal = [] if runner.get("argv") else ["-B", "test"]
                argv = ex + goal + ["-Dtest=" + ",".join(units), "-Dsurefire.failIfNoSpecifiedTests=false"]
    elif name == "gradle":
        if all_:
            argv = ex + ["test"]
        else:
            groups = {}
            for f, r in zip(files, rel):
                groups.setdefault(_gradle_project(top, runner, f), []).append(_jvm_class(r))
            for i in ids:
                f = i.get("file") if isinstance(i, dict) else None
                proj = _gradle_project(top, runner, f) if f else None
                groups.setdefault(proj, []).append(_id(i))
            argv = list(ex)
            for proj, filters in groups.items():
                argv.append("test" if proj is None else f"{proj}:test")
                for flt in dict.fromkeys(filters):
                    argv += ["--tests", flt]
    elif name == "go":
        argv = ex + ["-json"]
        if all_:
            argv.append("./...")
        else:
            dirs = {("./" + os.path.dirname(r)) if os.path.dirname(r) else "." for r in rel}
            tests = []
            for i in ids:
                d, _, t = _id(i).partition(":")
                d = _rel(top, runner, d) if d else "."
                dirs.add("./" + d if d != "." else ".")
                tests.append(t.split("/")[0] if t else None)
            if tests and None not in tests:
                argv += ["-run", "^(" + "|".join(dict.fromkeys(tests)) + ")$"]
            argv += sorted(dirs)
    elif name in ("phpunit", "pest"):
        argv = ex + (["--log-junit", report] if report else [])
        if not all_:
            if len(rel) == 1 and not ids:
                argv.append(rel[0])
            else:
                pats = [os.path.splitext(r.rsplit("/", 1)[-1])[0] for r in rel]
                pats += [_id(i).rsplit("\\", 1)[-1] for i in ids]
                argv += ["--filter", "(" + "|".join(dict.fromkeys(pats)) + ")"]
    else:
        raise ValueError(f"unknown runner {name}")
    return {"argv": argv, "cwd": _rootdir(top, runner), "env": env, "report": report, "names": names, "only": only}


# --------------------------------------------------------------------------- results


def _js_regex(text: str) -> str:
    """`text` as a JavaScript regular expression that matches it literally."""
    return re.sub(r"[.*+?^${}()|[\]\\/]", lambda m: "\\" + m.group(0), text)


def _pytest_module(rootdir, rel) -> str:
    """The name pytest's default import mode imports test file `rel` (relative to `rootdir`) by: its file name, after
    the names of the packages (folders with an __init__.py) that hold it."""
    parts = rel.replace("\\", "/").split("/")
    i = len(parts) - 1
    while i > 0 and os.path.isfile(os.path.join(rootdir, *parts[:i], "__init__.py")):
        i -= 1
    return ".".join(parts[i:-1] + [re.sub(r"\.py$", "", parts[-1])])


_IMPORTLIB = re.compile(r"--import-mode(?:=|[\s\"',]+)importlib\b")
# pytest's configuration files in the order it picks the first one, each with the section that makes it pytest's
_PYTEST_CONFIGS = (("pytest.ini", ""), (".pytest.ini", ""), ("pyproject.toml", "[tool.pytest"), ("tox.ini", "[pytest]"),
                   ("setup.cfg", "[tool:pytest]"))


def _pytest_importlib(top, runner, env=None) -> bool:
    """Does pytest import this runner's tests in importlib mode: on its command, in PYTEST_ADDOPTS, or in the addopts
    of the configuration file pytest reads (the first of pytest.ini, .pytest.ini, pyproject.toml, tox.ini and
    setup.cfg that configures pytest, in the runner's folder)?"""
    if _IMPORTLIB.search(" ".join(str(x) for x in runner.get("exec") or ())) or \
            _IMPORTLIB.search((env or {}).get("PYTEST_ADDOPTS") or os.environ.get("PYTEST_ADDOPTS") or ""):
        return True
    root = _rootdir(top, runner)
    for name, section in _PYTEST_CONFIGS:
        text = _read(os.path.join(root, name))
        if text and (not section or section in text):
            return bool(_IMPORTLIB.search(text))
    return False


def _pytest_buckets(top, runner, files, ids) -> list:
    """Test files (relative to the top) and node ids -> [(files, ids)], as few sets as possible in which no two test
    files share the name pytest imports them by."""
    rootdir = _rootdir(top, runner)
    buckets = []  # [({module name: its file}, files, ids)]
    for kind, item, rel in [("f", f, _rel(top, runner, f)) for f in files] + \
                           [("i", i, _id(i).split("::")[0]) for i in ids]:
        mod = _pytest_module(rootdir, rel)
        b = next((b for b in buckets if b[0].get(mod, rel) == rel), None)
        if b is None:
            b = ({}, [], [])
            buckets.append(b)
        b[0][mod] = rel
        (b[1] if kind == "f" else b[2]).append(item)
    return [(fs, i_s) for _, fs, i_s in buckets]


def _case(id_, file, line, status, message="", secs=0.0, name=""):
    return {"id": id_, "file": file, "line": line, "status": status, "message": (message or "")[:300],
            "secs": round(float(secs or 0), 3), "name": name or id_}


def _relto(top, path) -> str:
    if not path:
        return ""
    p = str(path)
    if os.path.isabs(p):
        try:
            r = os.path.relpath(os.path.realpath(p), os.path.realpath(top))
        except ValueError:
            return p
        return p if r.startswith("..") else r.replace("\\", "/")
    return os.path.normpath(p).replace("\\", "/")


def _first_line(text: str, skip=()) -> str:
    for ln in (text or "").splitlines():
        ln = ui.ANSI_RE.sub("", ln).strip()
        if ln and ln not in skip:
            return ln
    return ""


def _pytest_id(cls: str, name: str, base: str, top: str):
    parts = cls.split(".") if cls else []
    for i in range(len(parts), 0, -1):
        cand = "/".join(parts[:i]) + ".py"
        if os.path.exists(os.path.join(base, cand)):
            return "::".join([cand] + parts[i:] + [name]), _relto(top, os.path.join(base, cand))
    guess = "/".join(parts) + ".py" if parts else ""
    return "::".join([x for x in [guess, name] if x]), _relto(top, os.path.join(base, guess)) if guess else ""


def _jvm_file(cls: str, base: str, top: str, ext_hint: str = "") -> str:
    outer = cls.split("$")[0]
    rel = outer.replace(".", "/")
    exts = sorted((".java", ".kt", ".groovy", ".scala"), key=lambda e: e != ext_hint)
    for ext in exts:
        for src in ("src/test/java", "src/test/kotlin", "src/test/groovy", "src/test/scala",
                    "src/integrationTest/java", "src/integrationTest/kotlin"):
            cand = os.path.join(base, src, rel + ext)
            if os.path.exists(cand):
                return _relto(top, cand)
    return rel + ".java"


# What XML allows before the root element besides a document type declaration: a byte order mark, then whitespace, the
# XML declaration, processing instructions, and comments
_XML_PROLOG = re.compile(r"\ufeff?(?:\s+|<\?.*?\?>|<!--.*?-->)*", re.S)


def _junit_root(text):
    """The root element of a JUnit XML report, or None. A report never declares a document type, and the entities of
    one would expand while it is read (external entities, billion laughs): a declaration anywhere in the prolog, after
    comments of any length, refuses the report. `<!DOCTYPE` in a failure's text, after the prolog, is only text."""
    text = text.decode("utf-8", "replace") if isinstance(text, bytes) else (text or "")
    if text.startswith("<!", _XML_PROLOG.match(text).end()):
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def parse_junit(text, top, base, runner) -> list:
    """JUnit XML (pytest --junitxml, surefire/failsafe, gradle test-results, phpunit --log-junit) -> cases."""
    root = _junit_root(text)
    if root is None:
        return []
    out = []
    for tc in root.iter("testcase"):
        name = tc.get("name") or ""
        cls = tc.get("classname") or tc.get("class") or ""
        bad = tc.find("failure")
        bad = bad if bad is not None else tc.find("error")
        status = "failed" if bad is not None else "skipped" if tc.find("skipped") is not None else "passed"
        body = (bad.text or "") if bad is not None else ""
        msg = _first_line(bad.get("message") or "") if bad is not None else ""
        line = None
        if runner == "pytest" and not cls and bad is not None and name and \
                os.path.isfile(os.path.join(base, name.replace(".", "/") + ".py")):
            # a module pytest could not collect: no class, named by its dotted module; the error is in the body
            file = _relto(top, os.path.join(base, name.replace(".", "/") + ".py"))
            id_ = file
            hits = re.findall(r"(?m)^(\S+?\.py):(\d+):", body)
            pick = [h for h in hits if file.endswith(re.sub(r"^(?:\./)+", "", h[0]))]
            line = int(pick[-1][1]) if pick else None
            errs = re.findall(r"(?m)^E\s+(\S.*)$", body)
            msg = errs[-1].strip() if errs else msg
        elif runner == "pytest":
            id_, file = _pytest_id(cls, name, base, top)
            if tc.get("file"):
                file = _relto(top, os.path.join(base, tc.get("file")))
            hits = re.findall(r"(?m)^(\S+?\.py):(\d+):", body)
            pick = [h for h in hits if file.endswith(re.sub(r"^(?:\./)+", "", h[0]))] or hits
            line = int(pick[-1][1]) if pick else (int(tc.get("line")) + 1 if tc.get("line", "").isdigit() else None)
        elif runner in ("maven", "gradle"):
            meth = re.sub(r"\(.*$", "", name)
            id_ = f"{cls}#{meth}" if runner == "maven" else f"{cls}.{meth}"
            simple = cls.split(".")[-1].split("$")[0]
            m = re.search(r"\(" + re.escape(simple) + r"(\.(?:java|kt|groovy|scala)):(\d+)\)", body)
            file = _jvm_file(cls, base, top, m.group(1) if m else "")
            line = int(m.group(2)) if m else None
        else:  # phpunit, pest
            klass = tc.get("class") or cls.replace(".", "\\")
            id_ = f"{klass}::{name}"
            fabs = tc.get("file") or ""
            file = _relto(top, fabs)
            hits = re.findall(re.escape(fabs) + r":(\d+)", body) if fabs else []
            line = int(hits[-1]) if hits else (int(tc.get("line")) if (tc.get("line") or "").isdigit() else None)
            if not msg:
                msg = _first_line(re.sub(re.escape(fabs) + r":\d+", "", body) if fabs else body, skip=(id_,))
        if bad is not None and not msg:
            msg = _first_line(body)
        out.append(_case(id_, file, line, status, msg, tc.get("time") or 0, name))
    return out


def parse_playwright_junit(text, top, base, runner) -> list:
    """Playwright's JUnit report -> cases. A case's classname is its file relative to the testDir it was found in, its
    name the describe titles and the test's title joined by ' › '; the file is the one of the runner's test folders
    (`base`, the config's folder, without one) that holds it."""
    root = _junit_root(text)
    if root is None:
        return []
    dirs = [os.path.join(top, c) for c in runner.get("claims") or ()] or [base]
    out = []
    for tc in root.iter("testcase"):
        name, cls = tc.get("name") or "", tc.get("classname") or ""
        fabs = next((p for p in (os.path.join(d, cls) for d in dirs) if os.path.isfile(p)), os.path.join(dirs[0], cls))
        file = _relto(top, fabs)
        bad = tc.find("failure")
        bad = bad if bad is not None else tc.find("error")
        status = "failed" if bad is not None else "skipped" if tc.find("skipped") is not None else "passed"
        body = (bad.text or "") if bad is not None else ""
        hits = re.findall(re.escape(fabs) + r":(\d+):\d+", body)
        msg = _first_line(bad.get("message") or body) if bad is not None else ""
        out.append(_case(f"{file}::{name}", file, int(hits[-1]) if hits else None, status, msg,
                         tc.get("time") or 0, name))
    return out


def parse_node_junit(text, top, base, only="") -> list:
    """node --test --test-reporter=junit -> cases. A node:test case is `<file>::<title>`, inside its describe blocks
    joined by ' > '; a file that runs no node:test case (a plain script) is one case named by its path, which fails
    when the script exits non-zero. `base` is the folder node ran in. Node before 24 writes no `file` on a case:
    `only`, the absolute path of the one file node ran, stands in (run_tests runs node once per file for that)."""
    root = _junit_root(text)
    if root is None:
        return []
    out = []

    def case(tc, path):
        name, fabs = tc.get("name") or "", tc.get("file") or only or ""
        file = _relto(top, fabs)
        whole = bool(fabs) and not path and os.path.realpath(os.path.join(base, name)) == os.path.realpath(fabs)
        full = " > ".join(path + [name])
        bad = tc.find("failure")
        bad = bad if bad is not None else tc.find("error")
        status = "failed" if bad is not None else "skipped" if tc.find("skipped") is not None else "passed"
        msg, line = "", None
        if bad is not None:  # node ends an assertion's first line in ':' and gives the values (or a diff) after it
            lines = [ln.strip() for ln in (bad.get("message") or bad.text or "").splitlines() if ln.strip()]
            vals = [ln for ln in lines[1:] if ln != "+ actual - expected"][:2] if lines[:1] and \
                lines[0].endswith(":") else []
            msg = " ".join(lines[:1] + vals)
            code = re.search(r"\bexitCode: (\d+)", bad.text or "") if whole else None
            msg = f"the script exited {code.group(1)}" if code else msg
            m = re.search(re.escape(fabs) + r":(\d+):\d+", bad.text or "") if fabs else None
            line = int(m.group(1)) if m else None
        out.append(_case(file if whole else f"{file}::{full}", file, line, status, msg, tc.get("time") or 0,
                         file if whole else full))

    def walk(el, path):
        for ch in el:
            if ch.tag == "testsuite":
                walk(ch, path + [ch.get("name") or ""])
            elif ch.tag == "testcase":
                case(ch, path)

    walk(root, [] if root.tag == "testsuites" else [root.get("name") or ""])
    return out


def parse_go_json(text, top, runner) -> list:
    """`go test -json` events -> cases (one per top-level test; a package that failed without one is a case)."""
    module, root = runner.get("module") or "", runner.get("root") or "."

    def pkgdir(pkg: str) -> str:
        pkg = re.sub(r"\s*\[.*\]$", "", pkg or "")
        sub = pkg[len(module):].lstrip("/") if module and (pkg == module or pkg.startswith(module + "/")) else pkg
        return os.path.normpath(os.path.join(root, sub)).replace("\\", "/") if sub else (root if root != "." else ".")

    tests, pkgs = {}, {}
    for ln in (text or "").splitlines():
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        act, pkg, test = ev.get("Action"), ev.get("Package") or ev.get("ImportPath") or "", ev.get("Test")
        rec = tests.setdefault((pkg, test), {"status": None, "out": [], "secs": 0}) if test else \
            pkgs.setdefault(pkg, {"status": None, "out": [], "secs": 0})
        if act in ("output", "build-output"):
            rec["out"].append(ev.get("Output") or "")
        elif act in ("pass", "fail", "skip"):
            rec["status"], rec["secs"] = act, ev.get("Elapsed") or 0
    out = []
    failed_pkgs = set()
    for (pkg, test), rec in tests.items():
        if "/" in test:
            continue
        d = pkgdir(pkg)
        status = {"pass": "passed", "skip": "skipped"}.get(rec["status"], "failed")
        file, line, msg = d, None, ""
        for o in rec["out"]:
            m = re.match(r"^\s+([\w.\-]+_test\.go):(\d+): ?(.*)", o)
            if m:
                file, line, msg = f"{d}/{m.group(1)}" if d != "." else m.group(1), int(m.group(2)), m.group(3).strip()
                break
        if status == "failed":
            failed_pkgs.add(pkg)
            msg = msg or _first_line("".join(o for o in rec["out"] if not o.startswith(("=== ", "--- "))))
        out.append(_case(f"{d}:{test}", file, line, status, msg, rec["secs"], test))
    for pkg, rec in pkgs.items():
        if rec["status"] == "fail" and pkg not in failed_pkgs:
            d = pkgdir(pkg)
            text_ = "".join(rec["out"])
            m = re.search(r"(?m)^(?:\./)?([\w./\-]+\.go):(\d+)(?::\d+)?:", text_)
            file = os.path.normpath(os.path.join(root, m.group(1))).replace("\\", "/") if m else d
            msg = _first_line("\n".join(x for x in text_.splitlines()
                                        if not re.match(r"^(FAIL|ok|#)\s", x.strip())))
            out.append(_case(d, file, int(m.group(2)) if m else None, "failed", msg, rec["secs"], d))
    return sorted(out, key=lambda c: c["id"])


_JEST_STATUS = {"passed": "passed", "failed": "failed", "pending": "skipped", "skipped": "skipped", "todo": "skipped",
                "disabled": "skipped", "focused": "passed"}


def parse_jest_json(text, top) -> list:
    """jest --json / vitest --reporter=json -> cases."""
    try:
        data = json.loads(text) if isinstance(text, str) else text
    except ValueError:
        i = (text or "").find("{")
        try:
            data = json.loads(text[i:]) if i >= 0 else {}
        except ValueError:
            return []
    out = []
    for tr in (data or {}).get("testResults") or []:
        fabs = tr.get("name") or tr.get("testFilePath") or ""
        file = _relto(top, fabs)
        ars = tr.get("assertionResults") or []
        if not ars and tr.get("status") == "failed":
            out.append(_case(file, file, None, "failed", _first_line(tr.get("message") or tr.get("failureMessage")),
                             0, file))
        for ar in ars:
            full = ar.get("fullName") or " ".join((ar.get("ancestorTitles") or []) + [ar.get("title") or ""])
            msgs = "\n".join(ar.get("failureMessages") or [])
            m = re.search(re.escape(fabs) + r":(\d+):\d+", msgs) if fabs else None
            line = int(m.group(1)) if m else ((ar.get("location") or {}).get("line"))
            out.append(_case(f"{file}::{full}", file, line, _JEST_STATUS.get(ar.get("status"), "failed"),
                             _first_line(msgs), (ar.get("duration") or 0) / 1000.0, full))
    return out


_UT_HEAD = re.compile(r"^(FAIL|ERROR|UNEXPECTED SUCCESS): (\S+) \(([\w.]+)\)(.*)$")
# A test's line in the verbose list: `name (id) ... `, then what the test itself writes to stderr, then its result;
# alone on its line when a docstring line follows (`Its docstring. ... `)
_UT_START = re.compile(r"^(\S+) \(([\w.]+)\)(?:(?: [^\n]*?)?? \.\.\. (.*)|\s*)$")
# a test that starts right after one that wrote no result (Python 3.9 prints none for a test whose subtests failed)
_UT_NEXT = re.compile(r"^([A-Za-z_]\w*) \(([\w.]+)\) \.\.\. (.*)$")
# the result, the last thing written for a test, glued to its stderr when that ends without a newline
_UT_STATUS = re.compile(r"(ok|FAIL|ERROR|expected failure|unexpected success|skipped(?: '.*'| \".*\")?)\s*$")
_UT_STOP = re.compile(r"^(?:[=-]{20,}\s*$|Ran \d+ tests? in )")
_UT_FRAME = re.compile(r'^\s*File "(.+?)", line (\d+), in ')
_UT_LOADER = ("unittest.loader._FailedTest", "unittest.loader.ModuleImportFailure")


def _ut_id(name: str, paren: str, names=None) -> str:
    """A result's test id. A test unittest could not load carries only the last part of what it tried (test_x);
    `names`, the names asked for in order, gives the full one back: the first unused name with that part."""
    if paren.startswith(_UT_LOADER):
        for i, asked in enumerate(names or []):
            if name == asked or name in asked.split("."):
                return names.pop(i)
        return name
    if name in ("setUpClass", "tearDownClass", "setUpModule", "tearDownModule"):
        return paren
    return paren if paren.endswith("." + name) else f"{paren}.{name}"


def _ut_not_found(id_: str, message: str) -> bool:
    """unittest could not find the test named `id_` itself (its module or an attribute on the way is missing), as
    opposed to a test module that exists and fails to import."""
    parts = id_.split(".")
    m = re.match(r"(?:ModuleNotFoundError|ImportError): No module named '([\w.]+)'", message or "")
    if m:
        return parts[:len(m.group(1).split("."))] == m.group(1).split(".")
    m = re.match(r"AttributeError: .* has no attribute '(\w+)'", message or "")
    return bool(m) and m.group(1) in parts[1:]


def _ut_file(top, runner, id_: str) -> str:
    # ids are dotted from the start folder, or from the runner's root when the start folder is a package
    start = os.path.join(_rootdir(top, runner), runner.get("start") or ".")
    bases = [_rootdir(top, runner), start] if runner.get("package") else [start]
    parts = id_.split(".")
    for base in bases:
        for i in range(len(parts), 0, -1):
            cand = os.path.join(base, *parts[:i]) + ".py"
            if os.path.exists(cand):
                return _relto(top, cand)
    return ""


def _ut_results(lines) -> list:
    """The verbose list of a unittest run -> [(name, id in parentheses, result)]. Everything a test writes between its
    name and its result (its own stderr, which devkit reads with stdout) belongs to it; the result is what ends it."""
    out, cur, body, doc = [], None, [], False

    def close():
        said = [b for b in body if b.strip()]
        m = _UT_STATUS.search(said[-1]) if cur and said else None
        if m:
            out.append((cur[0], cur[1], m.group(1)))

    for ln in lines:
        m = _UT_START.match(ln)
        if m:
            close()
            cur, rest = (m.group(1), m.group(2)), m.group(3)
            while rest is not None and (n := _UT_NEXT.match(rest)):
                cur, rest = (n.group(1), n.group(2)), n.group(3)
            body, doc = ([] if rest is None else [rest]), rest is None
        elif _UT_STOP.match(ln):
            close()
            cur, body, doc = None, [], False
        elif cur:
            body.append(ln.split(" ... ", 1)[1] if doc and " ... " in ln else ln)
            doc = False
    close()
    return out


def parse_unittest(text, top, runner, names=()) -> list:
    """unittest's verbose output (3.9 and 3.11+ formats) -> cases, failures with file:line and the error line.
    `names` are the test names the command asked for: a test unittest could not load is reported by one of them,
    and marked `not_found` when the name itself no longer exists (renamed or deleted)."""
    lines = (text or "").splitlines()
    cases, pending = {}, list(names)
    for name, paren, st in _ut_results(lines):
        id_ = _ut_id(name, paren, pending)
        status = ("passed" if st == "ok" else "expected_failure" if st == "expected failure" else
                  "skipped" if st.startswith("skipped") else "failed")
        cases[id_] = _case(id_, _ut_file(top, runner, id_), None, status, "", 0, name)
    i, pending = 0, list(names)
    while i < len(lines):
        m = _UT_HEAD.match(lines[i])
        if not m:
            i += 1
            continue
        id_ = _ut_id(m.group(2), m.group(3), pending)
        j = i + 1
        while j < len(lines) and re.fullmatch(r"-{20,}", lines[j]) is None:
            j += 1
        body = []
        j += 1
        while j < len(lines) and not re.fullmatch(r"[=-]{20,}", lines[j]):
            body.append(lines[j])
            j += 1
        frames = [(k, fm) for k, fm in ((k, _UT_FRAME.match(b)) for k, b in enumerate(body)) if fm]
        mine = [(k, fm) for k, fm in frames if not _relto(top, fm.group(1)).startswith(("/", ".."))]
        file, line = _ut_file(top, runner, id_), None
        if mine:
            file, line = _relto(top, mine[-1][1].group(1)), int(mine[-1][1].group(2))
        after = frames[-1][0] + 1 if frames else 0
        msg = next((b.strip() for b in body[after:] if b.strip() and not b.startswith((" ", "\t"))), "") or \
            _first_line("\n".join(b for b in body if not b.startswith("Traceback")))
        prev = cases.get(id_)
        cases[id_] = _case(id_, file or (prev or {}).get("file", ""), line, "failed", msg, 0, m.group(2))
        # The innermost traceback may be production code. Keep the selected test's origin separate so proof
        # attribution does not replace its own error with another failure from the same test module.
        cases[id_]["test_file"] = _ut_file(top, runner, id_)
        if m.group(3).startswith(_UT_LOADER):
            cases[id_]["load_failed"] = True  # the test never ran: its module could not be imported or found
            if _ut_not_found(id_, msg):
                cases[id_]["not_found"] = True
        i = j
    return sorted(cases.values(), key=lambda c: c["id"])


# --------------------------------------------------------------------------- running

_NO_TESTS = re.compile(r"(?i)(no tests found|no tests to run|no tests ran|ran 0 tests|no tests executed|"
                       r"no test files found|no test suite found|no tests collected)")


def _walk(root, want_dirs):
    """Directories named in `want_dirs` under `root`, without descending into dependency folders or .git."""
    for d, subs, _ in os.walk(root):
        subs[:] = [s for s in subs if s not in SKIP_SEGMENTS and s != "src"]
        if os.path.basename(d) in want_dirs:
            yield d


def _jvm_reports(top, runner, since_ts) -> list:
    """Fresh surefire/failsafe (maven) or test-results (gradle) reports -> cases."""
    root, cases = _rootdir(top, runner), []
    want = {"surefire-reports", "failsafe-reports"} if runner["name"] == "maven" else {"test-results"}
    for d in _walk(root, want):
        for dirpath, _, files in os.walk(d):
            for fn in files:
                path = os.path.join(dirpath, fn)
                if not (fn.startswith("TEST-") and fn.endswith(".xml")):
                    continue
                try:
                    if os.path.getmtime(path) < since_ts - 1:
                        continue
                except OSError:
                    continue
                base = Path(path).parents[2] if runner["name"] == "maven" else Path(path).parents[3]
                cases += parse_junit(_read(path), top, str(base), runner["name"])
    return cases


def _collect(top, runner, cmd, output, t0) -> list:
    name, report = runner["name"], cmd.get("report")
    if name in ("pytest", "phpunit", "pest"):
        return parse_junit(_read(report), top, cmd["cwd"], name) if report and os.path.exists(report) else []
    if name in ("jest", "vitest", "ng"):
        return parse_jest_json(_read(report), top) if report and os.path.exists(report) else []
    if name == "playwright":
        return parse_playwright_junit(_read(report), top, cmd["cwd"], runner) if report and os.path.exists(report) \
            else []
    if name == "node":
        return parse_node_junit(_read(report), top, cmd["cwd"], cmd.get("only") or "") \
            if report and os.path.exists(report) else []
    if name == "unittest":
        return parse_unittest(output, top, runner, cmd.get("names") or ())
    if name == "go":
        return parse_go_json(output, top, runner)
    return _jvm_reports(top, runner, t0)


def _tail_line(output: str) -> str:
    lines = [ui.ANSI_RE.sub("", ln).strip() for ln in (output or "").splitlines()]
    lines = [ln for ln in lines if ln and not re.fullmatch(r"[=\-_*~. ]+", ln)]
    errs = [ln for ln in lines[-40:] if re.search(r"(?i)(error|exception|not found|no such|cannot|failed)", ln)]
    return (errs[0] if errs else lines[-1] if lines else "")[:300]


RUN_WORK = "devkit-run-"  # one test run's own files (command, reports, completion receipt), removed when it ends
_RUN_OWNER = re.compile(r"devkit-run-(\d+)-")


def _tmp_parent() -> Path:
    return Path(TMP_ROOT or core.TMP_ROOT)


def _unattributed_unittest_skips(text, cases) -> int:
    """Python 3.9 reports setup skips without a case name; retain the summary's count without inventing ids."""
    lines = text.rstrip().splitlines()
    summary = re.fullmatch(r"(?:OK|FAILED) \(([^)]*)\)", lines[-1]) if lines else None
    skipped = re.search(r"\bskipped=(\d+)\b", summary.group(1)) if summary else None
    return max(0, int(skipped.group(1)) - sum(c["status"] == "skipped" for c in cases)) if skipped else 0


_UNITTEST_OBSERVER = '''import json, os, sys, unittest

def run_observed_unittest(completion):
    class ObservedProgram(unittest.TestProgram):
        def runTests(self):
            try:
                return super().runTests()
            finally:
                result = getattr(self, "result", None)
                if result is not None:
                    with open(completion["path"], "w", encoding="utf-8") as output:
                        json.dump({"version": 1, "token": completion["token"],
                                   "tests": result.testsRun, "skipped": len(result.skipped),
                                   "successful": result.wasSuccessful(), "stopped": result.shouldStop}, output)
    ObservedProgram(module=None)
'''


def unittest_completion(work):
    """One invocation's outer-run receipt; test code is trusted, not sandboxed by this observer."""
    return {"path": str(Path(work) / "unittest-completion.json"), "token": os.urandom(16).hex()}


def observe_unittest(argv, work, completion):
    """Keep the interpreter, wrappers and unittest arguments; observe only this launcher's TestProgram."""
    argv = list(argv)
    module = next(i for i in range(len(argv) - 1) if argv[i:i + 2] == ["-m", "unittest"])
    script = Path(work) / "observed_unittest.py"
    script.write_text(_UNITTEST_OBSERVER + '''
completion = json.loads(sys.argv.pop(1))
sys.path[0] = os.getcwd()
sys.argv[0] = "python -m unittest"
run_observed_unittest(completion)
''')
    return argv[:module] + [str(script), json.dumps(completion)] + argv[module + 2:]


def observe_unittest_shell(command, work, completion):
    """Replace only the module words; bash still evaluates the original command's other words. In a multi-line step
    only the test command's line changes."""
    span = _unittest_span(command) if "\n" in command.strip() else None
    if span and span[1] - span[0] < len(command):  # one logical line (a continued one too) is lexed below
        start, end = span
        return command[:start] + observe_unittest_shell(command[start:end], work, completion) + command[end:]
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    words = []
    while True:
        start = lexer.instream.tell()
        word = lexer.get_token()
        if word is None:
            break
        end = lexer.instream.tell()
        while start < end and command[start].isspace():
            start += 1
        while end > start and command[end - 1].isspace():
            end -= 1
        words.append((word, start, end))
    module = next(i for i in range(len(words) - 1)
                  if [words[i][0], words[i + 1][0]] == ["-m", "unittest"])
    observed = observe_unittest(["-m", "unittest"], work, completion)
    return command[:words[module][1]] + shlex.join(observed) + command[words[module + 1][2]:]


_ISOLATED_UNITTEST = '''import json, os, sys, sysconfig, unittest
config = json.loads(open(sys.argv[1], encoding="utf-8").read())
startup = list(sys.path)
stdlib = [os.path.realpath(sysconfig.get_path(p)) for p in ("stdlib", "platstdlib")]
stdlib.append(os.path.join(sys.base_prefix, "lib", "python%d%d.zip" % sys.version_info[:2]))
roots = config["roots"]
eligible = [True]

def allowed(path):
    if not isinstance(path, str):
        return False
    if path.startswith("<") or path in ("built-in", "frozen"):
        return True
    path = os.path.realpath(path or os.getcwd())
    if any(p in ("site-packages", "dist-packages") for p in path.split(os.sep)):
        return False
    return any(path == root or path.startswith(root + os.sep) for root in roots + stdlib)

def observe(event, args):
    if event == "exec" and not allowed(getattr(args[0], "co_filename", None)):
        eligible[0] = False
    elif event == "import" and len(args) > 1 and args[1] and not allowed(args[1]):
        eligible[0] = False
    elif event == "open" and len(args) > 2 and isinstance(args[2], int):
        if args[2] & os.O_ACCMODE != os.O_WRONLY and not allowed(args[0]):
            eligible[0] = False
    elif event in ("subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "ctypes.dlopen"):
        eligible[0] = False

sys.addaudithook(observe)
if len(startup) > 64 or not all(allowed(p) for p in startup):
    eligible[0] = False
sys.path[:] = roots + [p for p in startup if allowed(p)]
sys.dont_write_bytecode = True
sys.argv[:] = ["unittest"] + config["args"]
try:
    run_observed_unittest(config["completion"])
finally:
    if len(sys.path) > 64 or not all(allowed(p) for p in sys.path):
        eligible[0] = False
    modules = list(sys.modules.items())
    if len(modules) > 4096:
        eligible[0] = False
    for name, module in modules[:4096]:
        if name == "__main__":
            continue
        origin = getattr(module, "__file__", None)
        spec = getattr(module, "__spec__", None)
        origin = origin or getattr(spec, "origin", None)
        if origin and not allowed(origin):
            eligible[0] = False
        paths = list(getattr(module, "__path__", ()))
        if len(paths) > 64 or not all(allowed(p) for p in paths):
            eligible[0] = False
    with open(config["receipt"], "w", encoding="utf-8") as output:
        json.dump({"imports_attested": eligible[0]}, output)
'''


def _direct_unittest(runner):
    return (runner["name"] == "unittest" and runner.get("exec") == [sys.executable, "-m", "unittest"]
            and Path(runner.get("python") or "").resolve() == Path(sys.executable).resolve())


def _isolate_unittest(top, runner, cmd, work, completion):
    """Fixed launcher: no site customization, inherited Python settings or editable-install search paths."""
    if not _direct_unittest(runner):
        raise ValueError("isolated imports require direct unittest on the current interpreter")
    root = Path(top).resolve()
    folder = Path(_rootdir(top, runner)).resolve()
    roots = []
    for path in (folder / (runner.get("start") or "."), folder, folder / "src", root, root / "src"):
        path = path.resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue  # an editable/symlinked source outside the checkout is not an isolated source root
        if path.is_dir() and str(path) not in roots:
            roots.append(str(path))
    script, config, receipt = work / "isolated_unittest.py", work / "imports.json", work / "import-receipt.json"
    script.write_text(_UNITTEST_OBSERVER + _ISOLATED_UNITTEST)
    config.write_text(json.dumps({"roots": roots, "args": cmd["argv"][3:], "receipt": str(receipt),
                                 "completion": completion}))
    cmd["argv"] = [sys.executable, "-I", "-S", str(script), str(config)]
    return receipt


def run_complete(runner, rc, completion=None, failures=()) -> bool:
    """Whether the runner finished its selection, rather than merely reporting some test results.

    unittest completion belongs to the launcher's TestProgram, never to a nested runner or text a test prints.
    Other runners write no completion receipt: a run that exits 0, or exits 1 with the failures it reports
    (`failures`), finished its selection. A runner that stops at its first failure (failfast) did not.
    """
    if rc != 0 and runner.get("failfast"):
        return False
    if runner["name"] == "unittest":
        if rc not in (0, 1, 5):
            return False
        if (not isinstance(completion, dict) or not isinstance(completion.get("path"), str)
                or not isinstance(completion.get("token"), str) or not completion["token"]):
            return False
        receipt = core.read_json(completion["path"], {})
        if (not isinstance(receipt, dict) or type(receipt.get("version")) is not int or receipt["version"] != 1
                or receipt.get("token") != completion["token"] or type(receipt.get("tests")) is not int
                or receipt["tests"] < 0 or type(receipt.get("skipped")) is not int or receipt["skipped"] < 0
                or type(receipt.get("successful")) is not bool or receipt.get("stopped") is not False):
            return False
        return ((rc == 0 and receipt["successful"]) or (rc == 1 and not receipt["successful"])
                or (rc == 5 and receipt["successful"] and receipt["tests"] == 0 and receipt["skipped"] == 0))
    return rc == 0 or (rc == 1 and bool(failures))


def run_tests(top, runner, files=(), ids=(), related=(), all_=False, env=None, stream=False, log=None,
              timeout=None, isolated_imports=False) -> dict:
    """Run one selection with one runner; stream the output when asked; parse the results.

    -> {runner, root, files, ids, related, all, argv, rc, secs, cases, count, passed, failed, skipped, expected_failure,
        failures, zero (the selection ran no test), complete (the selection finished), crash (no complete result),
        not_found (names asked for that no longer exist), unattributed_skips (native skips with no case identity),
        log}"""
    top = str(top)
    files, ids, related = list(files), list(ids), list(related)
    if runner["name"] == "node" and not all_:
        # Node before 24 names no file in its JUnit cases, so a case of a run over several files belongs to none of
        # them: run node once per test file, each case then belongs to its run's file
        targets = list(dict.fromkeys(files + [_id(i).split("::")[0] for i in ids if _id(i).split("::")[0] not in files]))
        if len(targets) > 1:
            parts = []
            for k, f in enumerate(targets):
                part_log = f"{log}.{k}" if log else None
                parts.append(run_tests(top, runner, [f] if f in files else [],
                                       [i for i in ids if _id(i).split("::")[0] == f], env=env, stream=stream,
                                       log=part_log, timeout=timeout))
            return _merged_runs(parts, files, ids, log)
    if runner["name"] == "pytest" and not related and not _pytest_importlib(top, runner, env):
        # pytest's default import mode imports a test file by its name inside its packages: two test files of one
        # process with the same such name (tests/unit/x/test_api.py, tests/integration/x/test_api.py, no
        # __init__.py) fail to collect. Each set of files with distinct names runs in its own process. In importlib
        # mode no such names clash, and the run stays one, with pytest's own collection for the whole suite.
        if all_:
            repo = _Repo(top)
            picked = [t for t in repo.tests if (owner(repo.runners(), t) or {}).get("root") == runner["root"]
                      and _lang(t) == "py"]
        else:
            picked = files
        buckets = _pytest_buckets(top, runner, picked, [] if all_ else ids)
        if len(buckets) > 1:
            parts = [run_tests(top, runner, fs, i_s, env=env, stream=stream, log=f"{log}.{k}" if log else None,
                               timeout=timeout) for k, (fs, i_s) in enumerate(buckets)]
            merged = _merged_runs(parts, [] if all_ else files, [] if all_ else ids, log)
            merged["all"] = bool(all_)
            return merged
    # The test process writes the completion receipt here: outside devkit's state folder, which a suite may guard
    # (devkit's own hermetic guard refuses every write below it), so a green run is never reported incomplete
    tmpd = _tmp_parent()
    tmpd.mkdir(parents=True, exist_ok=True)
    sweep_run_folders(tmpd)  # those of killed runs: a run removes its own when it ends
    work = Path(tempfile.mkdtemp(prefix=f"{RUN_WORK}{os.getpid()}-", dir=str(tmpd)))
    report = None
    if runner["name"] in ("pytest", "phpunit", "pest", "node", "playwright"):
        report = str(work / "junit.xml")
    elif runner["name"] in ("jest", "vitest", "ng"):
        report = str(work / "report.json")
    cmd = build_command(top, runner, files, ids, report, related, all_)
    from . import identity
    full_env = dict(os.environ)
    full_env.update(env or {})
    for k, v in cmd["env"].items():
        full_env[k] = v if k != "PYTHONPATH" or not (env or {}).get(k) else v + os.pathsep + env[k]
    full_env = identity.runner_env(full_env)  # the tests are the project's code: a dev command in them names nobody
    import_receipt = None
    completion = unittest_completion(work) if runner["name"] == "unittest" else None
    if isolated_imports:
        import_receipt = _isolate_unittest(top, runner, cmd, work, completion)
        full_env = {k: v for k, v in full_env.items() if not k.startswith("PYTHON")}
    elif completion:
        cmd["argv"] = observe_unittest(cmd["argv"], work, completion)
    log = Path(log) if log else work / "output.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    out, t0 = [], time.time()
    try:
        # A run with a limit (dev ci --run --resume's partial rerun) gets a session of its own, so the limit or an
        # interrupt stops every process it started, as the full step runner does (gitx._step_shell): killing only
        # the runner left its workers and the tests' children running, holding the output open past the limit.
        # nosemgrep: python36-compatibility-Popen1 (devkit needs Python 3.9+, where Popen takes errors=)
        with open(log, "w") as fh, subprocess.Popen([str(x) for x in cmd["argv"]], cwd=cmd["cwd"], env=full_env,
                                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                                   errors="replace", start_new_session=bool(timeout)) as proc:
            timer = None
            if timeout:
                import threading
                from .gitx import _stop_group
                timer = threading.Timer(timeout, _stop_group, (proc,))
                timer.daemon = True
                timer.start()
            try:
                for ln in proc.stdout:
                    out.append(ln)
                    fh.write(ln)
                    if stream:
                        sys.stdout.write(ln)
                        sys.stdout.flush()
                rc = proc.wait()
            except BaseException:
                if timeout:
                    _stop_group(proc)
                raise
            finally:
                if timer:
                    timer.cancel()
    except OSError as e:
        out.append(f"{cmd['argv'][0]}: {e}\n")
        log.write_text("".join(out))
        rc = 127
    secs = time.time() - t0
    text = "".join(out)
    cases = {}
    for c in _collect(top, runner, cmd, text, t0):
        if c["id"] not in cases or c["status"] == "failed":
            cases[c["id"]] = c
    # a name asked for that no longer exists (renamed or deleted) selects no test: it is not a failure of this run
    not_found = [c["id"] for c in cases.values() if c.get("not_found")]
    cases = [c for c in cases.values() if not c.get("not_found")]
    imports_attested = bool(import_receipt and
                           (core.read_json(import_receipt, {}) or {}).get("imports_attested") is True)
    failures = [c for c in cases if c["status"] == "failed"]
    unit = runner["name"] == "unittest"
    unattributed = _unattributed_unittest_skips(text, cases) if unit else 0
    complete = run_complete(runner, rc, completion, failures)
    if unit and not complete:
        # Loader diagnostics and nested summaries cannot prove that the outer selection was exhausted.
        not_found = []
    if complete and completion:
        receipt = core.read_json(completion["path"], {})
        unattributed = max(0, receipt["skipped"] - sum(c["status"] == "skipped" for c in cases))
    shutil.rmtree(work, ignore_errors=True)
    if unit:
        # only the launcher's receipt proves an empty selection: loader text and nested summaries cannot
        zero = not cases and not unattributed and complete
    else:
        # a name that is gone, or a runner that says no test ran, selected nothing whatever its exit code (pytest
        # exits 4 for a node id that no longer exists)
        zero = not cases and (rc in (0, 5) or bool(not_found) or bool(_NO_TESTS.search(text)))
    crash = False
    py = runner.get("exec") or [""]
    if not cases and not unattributed and not zero and runner["name"] == "pytest" and py[1:3] == ["-m", "pytest"] \
            and re.search(r"No module named '?pytest'?(?:\s|$)", text):
        # the environment found for the package lacks pytest: one runner problem, no failure of any test
        crash = True
        failures = [_case(f"pytest:{runner['root']}", "", None, "failed",
                          f"pytest is not installed for {py[0]}: install it in that environment (uv sync, or "
                          f"{py[0]} -m pip install pytest), or give the package a .venv that has it")]
    elif not cases and not unattributed and not zero:
        crash = True
        # A missing outer result is a runner failure, not a new assertion against every requested test.
        units = [f"unittest:{runner['root']}"] if unit else ids or files or [runner["name"]]
        failures = [_case(_id(u), (u.get("file") if isinstance(u, dict) else u if u in files else ""), None,
                          "failed", f"{runner['name']} exited {rc}: {_tail_line(text)}") for u in units]
    elif not zero and ((unit and not complete) or (rc != 0 and not failures and not not_found)):
        # unittest without its receipt, or any runner that failed without naming a failed test
        crash = True
        failures.append(_case(f"{runner['name']}:{runner['root']}", "", None, "failed",
                              f"{runner['name']} exited {rc} without a complete result after {len(cases)} test(s): "
                              f"{_tail_line(text)}"))
    count = len(cases) + unattributed
    complete = complete and not crash
    return {**({"imports_attested": imports_attested} if isolated_imports else {}),
            "runner": runner["name"], "root": runner["root"], "files": files, "ids": [_id(i) for i in ids],
            "source": runner.get("source") or "inferred", "declared_by": runner.get("declared_by") or "",
            "related": related, "all": bool(all_), "argv": [str(x) for x in cmd["argv"]], "cwd": cmd["cwd"],
            "rc": rc, "secs": round(secs, 2), "cases": cases, "count": count,
            "passed": sum(c["status"] == "passed" for c in cases), "failed": len(failures),
            "skipped": sum(c["status"] == "skipped" for c in cases) + unattributed,
            "unattributed_skips": unattributed, "failures": failures, "zero": zero,
            "expected_failure": sum(c["status"] == "expected_failure" for c in cases),
            "complete": complete, "crash": crash, "not_found": not_found,
            "log": str(log) if log.parent != work else ""}


def _merged_runs(parts, files, ids, log) -> dict:
    """The runs of one selection, one per node test file, as one run (run_tests' result)."""
    if log:
        with open(log, "w") as out:
            for p in parts:
                if p["log"] and os.path.exists(p["log"]):
                    out.write(Path(p["log"]).read_text(errors="replace"))
                    os.remove(p["log"])
    cases = [c for p in parts for c in p["cases"]]
    failures = [c for p in parts for c in p["failures"]]
    return {**parts[0], "files": list(files), "ids": [_id(i) for i in ids],
            "argv": [x for p in parts for x in p["argv"] + [";"]][:-1],
            "rc": next((p["rc"] for p in parts if p["rc"]), 0), "secs": round(sum(p["secs"] for p in parts), 2),
            "cases": cases, "count": sum(p["count"] for p in parts),
            "passed": sum(p["passed"] for p in parts), "failed": len(failures),
            "skipped": sum(p["skipped"] for p in parts), "failures": failures, "zero": all(p["zero"] for p in parts),
            "expected_failure": sum(p.get("expected_failure", 0) for p in parts),
            "unattributed_skips": sum(p.get("unattributed_skips", 0) for p in parts),
            "complete": all(p.get("complete") is True for p in parts),
            "crash": any(p["crash"] for p in parts), "not_found": [n for p in parts for n in p["not_found"]],
            "log": str(log) if log else ""}


# --------------------------------------------------------------------------- recording


def project_key(top):
    """-> (project id, file-safe key): the same identity devkit uses everywhere (owner/repo, or _local/<name>)."""
    remote = _git(top, "config", "--get", "remote.origin.url")
    common = _git(top, "rev-parse", "--git-common-dir")
    if common and not os.path.isabs(common):
        common = os.path.join(top, common)
    main = Path(common).resolve().parent if common else Path(top)
    pid = core.project_identity(main, remote)[0]
    return pid, re.sub(r"[^A-Za-z0-9._-]+", "__", pid)


def state_path(top) -> Path:
    return core.STATE / "tests" / f"{project_key(top)[1]}.json"


def load_state(top) -> dict:
    data = core.read_json(state_path(top), {})
    return data if isinstance(data, dict) else {}


def _update_state(top, change) -> dict:
    """Read, change and write the project's record under its lock: every worktree and lane of the project shares
    it, so a write never drops another run's results, and readers never see a half-written file."""
    path = state_path(top)
    path.parent.mkdir(parents=True, exist_ok=True)
    with core.Lock(f"tests-{project_key(top)[1]}", wait=30):
        st = load_state(top)
        change(st)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(st, indent=1))
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return st


_UT_DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


def _loadable(runner, root, id_) -> bool:
    """Can `runner` load the test named `id_` again? Not the label of a run that failed with no test to name
    (`unittest:.`, the runner and its folder), and for unittest only a dotted name or a test file."""
    if not id_ or id_ == f"{runner}:{root}":
        return False
    return runner != "unittest" or bool(_UT_DOTTED.fullmatch(id_)) or id_.endswith(".py")


def _rerunnable(res, case) -> bool:
    """A failure can be recorded only with its module or file (a bare unittest name would rerun as a module), and
    only by a name its runner can load."""
    return _loadable(res["runner"], res["root"], case["id"]) and \
        (bool(case["file"]) or res["runner"] != "unittest" or "." in case["id"])


def record(top, results, kind="run") -> dict:
    """Fold run results into the project's record: failures replaced for what ran, durations, full-run time."""
    top = str(top)
    return _update_state(top, lambda st: _fold(top, st, results, kind))


def _fold(top, st, results, kind) -> None:
    st["project"] = project_key(top)[0]
    fails = list((st.get("failures") or {}).get(top) or [])
    from . import gitx
    tree = gitx.work_tree(top) or ""
    now = int(time.time())
    for res in results:
        if res["zero"]:
            continue
        ran_ids = {c["id"] for c in res["cases"]} | set(res["ids"])
        ran_files = set(res["files"]) | {c["file"] for c in res["cases"]}
        unresolved = {c["id"] for c in res["cases"] if c["status"] in ("skipped", "expected_failure")}
        resolved = {c["id"] for c in res["cases"] if c["status"] in ("passed", "failed")}
        keep = []
        for f in fails:
            same = f.get("runner") == res["runner"] and f.get("root") == res["root"]
            # Requested names and files are not execution evidence after interruption. Only an observed passing
            # or failing result may replace an earlier failure; keep everything the runner did not reach.
            if same and res.get("complete") is not True and f.get("id") not in resolved:
                keep.append(f)
                continue
            # A skipped case or unittest setup did not resolve an earlier failure. An explicit result for the
            # method still takes precedence over a skipped teardown of its class or module.
            if same and f.get("id") not in resolved and (res.get("unattributed_skips") or any(
                    f.get("id") == i or (res["runner"] == "unittest" and f.get("id", "").startswith(i + "."))
                    for i in unresolved)):
                keep.append(f)
                continue
            if same and (res["all"] or f.get("id") in ran_ids or (not res["ids"] and f.get("file") in ran_files)):
                continue
            keep.append(f)
        fails = keep + [{"runner": res["runner"], "root": res["root"], "id": c["id"], "file": c["file"],
                         "line": c["line"], "message": c["message"], "ts": now} for c in res["failures"]
                        if _rerunnable(res, c)]
        rk = f"{res['runner']}:{res['root']}"
        durs = st.setdefault("durations", {}).setdefault(rk, {})
        per = {}
        for c in res["cases"]:
            if c["file"]:
                per[c["file"]] = per.get(c["file"], 0) + c["secs"]
        if per and sum(per.values()) > 0:
            durs.update({k: round(v, 3) for k, v in per.items()})
        elif res["files"] and not res["ids"] and res.get("complete") is True:
            durs.update({f: round(res["secs"] / len(res["files"]), 3) for f in res["files"]})
        if res["all"] and res.get("complete") is True:
            st.setdefault("full", {})[rk] = {"secs": res["secs"], "tree": tree, "ts": now,
                                             **{k: res.get(k, 0) for k in
                                                ("count", "passed", "failed", "skipped", "expected_failure",
                                                 "unattributed_skips")}}
    st.setdefault("failures", {})[top] = fails
    st["last"] = {"top": top, "kind": kind, "tree": tree, "ts": now,
                  "complete": all(r.get("complete") is True for r in results),
                  **{k: sum(r.get(k, 0) for r in results) for k in
                     ("count", "passed", "failed", "skipped", "expected_failure", "unattributed_skips")},
                  "secs": round(sum(r["secs"] for r in results), 2)}
    st["tree"], st["ts"] = tree, now


def _forget(top, stale) -> None:
    """Drop recorded failures that no longer select a test (renamed or deleted)."""
    def drop(st):
        fails = (st.get("failures") or {}).get(str(top)) or []
        st.setdefault("failures", {})[str(top)] = [f for f in fails if (f.get("runner"), f.get("root"), f.get("id"))
                                                   not in stale]
    _update_state(top, drop)


def estimate(st, runner, files=(), all_=False, n_files=0):
    """Seconds from history, or None when nothing is known."""
    rk = f"{runner['name']}:{runner['root']}"
    full = (st.get("full") or {}).get(rk)
    durs = (st.get("durations") or {}).get(rk) or {}
    if all_:
        return full["secs"] if full else (round(sum(durs.values()), 1) if durs else None)
    files = list(files)
    known = [durs[f] for f in files if f in durs]
    unknown = len(files) - len(known)
    if unknown and full and n_files:
        return round(sum(known) + unknown * full["secs"] / n_files, 1)
    if durs:  # files never timed: the average of the runner's timed files
        return round(sum(known) + unknown * sum(durs.values()) / len(durs), 1)
    return None


def _fmt(secs) -> str:
    if secs is None:
        return "unknown"
    s = float(secs)
    return f"{s:.1f}s" if s < 60 else f"{int(s) // 60}m{int(s) % 60:02d}s"


# --------------------------------------------------------------------------- output


def _refuse(reason, fix=None, code=2) -> int:
    print(f"{ui.FAIL} {reason}", file=sys.stderr)
    if fix:
        print(f"  {ui.ARROW} {fix}", file=sys.stderr)
    return code


def _label(res) -> str:
    if res["all"]:
        return "all tests"
    if res["related"]:
        return f"related to {len(res['related'])} changed file(s)"
    if res["ids"] and not res["files"]:
        return res["ids"][0] if len(res["ids"]) == 1 else f"{len(res['ids'])} tests"
    fs = res["files"]
    return ", ".join(fs) if len(fs) <= 3 else f"{len(fs)} files"


def _show(res, top):
    ok = not res["failures"] and not res["zero"]
    counts = f"{res['count']} test(s), {res['passed']} passed"
    for key, label in (("failed", "failed"), ("skipped", "skipped"), ("expected_failure", "expected failure(s)")):
        if res.get(key):
            counts += f", {res[key]} {label}"
    mark = (ui.OK if res["passed"] else ui.WARN) if ok else ui.FAIL
    print(f"  {mark} {res['runner']} · {_label(res)} · {counts} · {_fmt(res['secs'])}")
    for c in res["failures"][:10]:
        where = f"{c['file']}:{c['line']}" if c["file"] and c["line"] else c["file"] or c["id"]
        print(f"      {where}  {ui.dim(c['id']) if where != c['id'] else ''}".rstrip())
        if c["message"]:
            print(f"        {c['message'][:200]}")
    if len(res["failures"]) > 10:
        print(f"      … {len(res['failures']) - 10} more")
    if (res["failures"] or res["zero"]) and res["log"]:
        print(ui.dim(f"      full log: {core.tilde(res['log'])}"))


def _summary(res) -> dict:
    out = {k: res[k] for k in ("runner", "root", "files", "ids", "related", "all", "argv", "rc", "secs", "count",
                               "passed", "failed", "skipped", "zero", "crash", "log")}
    out["source"], out["declared_by"] = res.get("source") or "inferred", res.get("declared_by") or ""
    out["not_found"] = list(res.get("not_found") or [])
    out["complete"] = res.get("complete") is True
    out["expected_failure"] = res.get("expected_failure", 0)
    out["unattributed_skips"] = res.get("unattributed_skips", 0)
    out["failures"] = [{k: c[k] for k in ("id", "file", "line", "message")} for c in res["failures"]]
    return out


def _zero_problem(res, changed=()) -> int:
    sel = " ".join(shlex.quote(x) for x in res["argv"])
    what = _label(res)
    more = f" · changed: {', '.join(list(changed)[:5])}" if changed else ""
    return _refuse(f"selection problem: {res['runner']} ran 0 tests for {what} (a selector that matches nothing is "
                   f"not a pass)", f"selector: {core.tilde(sel)}{more} · check the names, or see dev test plan")


# --------------------------------------------------------------------------- dev test / dev test run


def _top():
    return _git(os.getcwd(), "rev-parse", "--show-toplevel")


def _runners(top, a):
    found = detect(top)
    want = getattr(a, "runner", None)
    if want:
        found = [r for r in found if r["name"] == want]
    return found


def _has_bin(top, runner, tool) -> bool:
    for d in (Path(_rootdir(top, runner)), Path(top)):
        if (d / "node_modules" / ".bin" / tool).exists():
            return True
    return False


def _group_tests(top, runners, tests, changed=()):
    """Test files -> [(runner, {files, related})]; jest/vitest use their own related mode when installed."""
    groups, orphans = {}, []
    for t in tests:
        r = owner(runners, t)
        if r is None:
            orphans.append(t)
            continue
        groups.setdefault(id(r), (r, []))[1].append(t)
    out = []
    every = {}
    for t in _Repo(top).tests if groups else []:
        o = owner(runners, t)
        if o is not None:
            every.setdefault(id(o), set()).add(t)
    for r, files in groups.values():
        # the selection is the whole suite: run it as one (timed); node's suite is what its test script names, which
        # need not be the files devkit sees, so node always runs the files selected
        if set(files) >= every.get(id(r), {None}) and r["name"] != "node":
            out.append((r, {"all": True, "files": [], "related": []}))
            continue
        rel = []
        if r["name"] in ("jest", "vitest") and _has_bin(top, r, r["name"]):
            rel = [f for f in changed if _lang(f) == "js" and _under(f, r["root"]) and os.path.exists(
                os.path.join(top, f))]
        out.append((r, {"files": [] if rel else sorted(files), "related": rel}))
    return out, orphans


def _unloadable(top, st) -> list:
    """Recorded failures whose names no runner can load (an older devkit recorded them): [(runner, root, id)]."""
    return [(f.get("runner"), f.get("root"), f.get("id")) for f in (st.get("failures") or {}).get(top) or []
            if not _loadable(f.get("runner"), f.get("root"), f.get("id"))]


def _deleted(top, st) -> list:
    """Recorded failures whose test file is gone from the work tree (deleted, or renamed with the file): no run can
    select them again: [(runner, root, id)]."""
    return [(f.get("runner"), f.get("root"), f.get("id")) for f in (st.get("failures") or {}).get(top) or []
            if f.get("file") and not os.path.exists(os.path.join(top, f["file"]))]


def _not_tests(top, st) -> list:
    """Recorded failures in a file named like a test that is no test here (a production module test_x.py outside
    pytest's testpaths, which an older devkit selected and recorded): it is never selected again, and running its
    names by hand fails on fixtures no test gives them: [(runner, root, id)]."""
    fails = [f for f in (st.get("failures") or {}).get(top) or [] if f.get("file") and _runnable(f["file"])
             and os.path.exists(os.path.join(top, f["file"]))]
    if not fails:
        return []
    repo = _Repo(top)
    return [(f.get("runner"), f.get("root"), f.get("id")) for f in fails if not repo.runnable(f["file"])]


def _say_gone(gone) -> None:
    if gone:
        print(f"{ui.WARN} {len(gone)} recorded failure(s) no longer select a test (renamed or deleted): "
              f"forgotten: {', '.join(gone[:5])}" + (f" and {len(gone) - 5} more" if len(gone) > 5 else ""),
              file=sys.stderr)


def _failed_groups(top, runners, st):
    no_test = {i for _, _, i in _not_tests(top, st)}
    fails = [f for f in (st.get("failures") or {}).get(top) or []
             if (not f.get("file") or os.path.exists(os.path.join(top, f["file"])))
             and _loadable(f.get("runner"), f.get("root"), f.get("id")) and f.get("id") not in no_test]
    out = []
    for r in runners:  # runners that share a root (an Angular workspace's projects) take the failures of their files
        mine = [f for f in fails if f.get("runner") == r["name"] and f.get("root") == r["root"] and
                ("claims" not in r or not f.get("file") or owner(runners, f["file"]) is r)]
        if mine:
            out.append((r, mine))
    return out


def _run_step(top, groups, stream, json_out, kind, header):
    if not json_out:
        print(ui.section(header))
    results = []
    for r, sel in groups:
        with _run_log(top, r) as log:
            res = run_tests(top, r, files=sel.get("files", ()), ids=sel.get("ids", ()), related=sel.get("related", ()),
                            all_=sel.get("all", False), stream=stream, log=log)
            results.append(res)
            if not json_out:
                _show(res, top)
    return {"kind": kind, "runs": results}


_LOGS_KEPT = 10  # per worktree, runner and root


def _log_path(top, runner, codex=False) -> Path:
    """The stem of a run's log (<folder>/<runner>-<root>; each run adds its time and a unique part): a folder per
    worktree (lanes share the project). Codex's logs have a folder of their own, which the unlocked cleaner of older
    devkit copies does not descend into."""
    key = project_key(top)[1]
    tree = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(os.path.normpath(str(top))))[:40]
    tree += "-" + hashlib.sha256(os.path.realpath(str(top)).encode()).hexdigest()[:8]
    root = re.sub(r"[^A-Za-z0-9._-]+", "_", runner["root"])
    d = core.STATE / "tests" / "logs" / key / tree
    stem = f"{runner['name']}-{root}"
    return d / "codex" / stem if codex else d / stem


@contextlib.contextmanager
def _run_log(top, runner):
    """Own the run's log until the runner and its report finish: it is created and locked under its folder's lock,
    and retention, under the same lock, removes only completed logs, so a long run keeps its log however many
    shorter runs start and end meanwhile (Codex and every other caller alike)."""
    from . import progress
    agent, _, how = progress.resolve_session(str(top))
    base = _log_path(top, runner, codex=agent == "codex" and how not in ("recency", "shell"))
    base.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    try:
        # Publication and cleaning share a lock, so no cleaner sees a newly created file before its owner locks it.
        with core.file_lock(base.parent) as held:
            if not held:
                raise OSError("test log allocation is busy; retry dev test when the other allocation finishes")
            fd, name = tempfile.mkstemp(prefix=f"{base.name}-{time.strftime('%Y%m%d-%H%M%S')}-",
                                        suffix=".log", dir=str(base.parent))
            log = Path(name)
            fcntl.flock(fd, fcntl.LOCK_EX)
            _prune_logs(base)
        try:
            yield log
        finally:
            with core.file_lock(base.parent) as held:
                if held:
                    # Completed logs age from completion, so a long run keeps its report after shorter peers end.
                    with contextlib.suppress(OSError):
                        os.utime(log, None)
                    os.close(fd)
                    fd = None
                    _prune_logs(base, keep=log)
    finally:
        if fd is not None:
            os.close(fd)  # errors and interrupted runs release ownership too; the next run can prune their logs


def _prune_logs(base, keep=None):
    """Under the directory lock, retain ten completed logs and every live writer (an actual flock, never a PID)."""
    with contextlib.ExitStack() as opened:
        completed = []
        for path in base.parent.glob(f"{base.name}-*.log"):
            try:
                handle = opened.enter_context(path.open("r"))
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                completed.append((path == keep, path.stat().st_mtime_ns, str(path), path))
            except OSError:
                continue  # a live writer or a file that disappeared is not ours to prune
        for _, _, _, path in sorted(completed)[:-_LOGS_KEPT]:
            with contextlib.suppress(OSError):
                path.unlink()


def _emit(data) -> None:
    for s in data.get("steps") or []:
        s["runs"] = [_summary(r) for r in s["runs"]]
    print(json.dumps(data, ensure_ascii=False, indent=1))


def _advice(st, radius, runners, json_out, top, ran=()):
    """The changes can affect every test: say why, what was left out and how long the full run takes."""
    forced = ("every test", "widened to all", "widened to the tests of")
    why = {}
    for f, w in radius["reasons"]:
        if any(x in w for x in forced):
            why.setdefault(w, []).append(f)
    scope = radius.get("all_roots") or ["."]
    scoped = [r for r in runners if "." in scope or r["root"] in scope]
    tests = _Repo(top).tests
    left = [t for t in tests if owner(scoped, t) is not None and t not in set(ran)]
    if not left:
        return None
    est = [estimate(st, r, all_=True) for r in scoped]
    total = None if any(e is None for e in est) else sum(est)
    when = max([((st.get("full") or {}).get(f"{r['name']}:{r['root']}") or {}).get("ts") or 0 for r in scoped] + [0])
    hist = f"~{_fmt(total)} (last full run {core.ago(when)})" if total is not None and when else \
        f"~{_fmt(total)} from recorded durations" if total is not None else "no timing yet"
    causes = "; ".join(f"{fs[0]}" + (f" and {len(fs) - 1} more" if len(fs) > 1 else "") + f" ({w})"
                       for w, fs in list(why.items())[:3])
    text = (f"not run: {len(left)} more test file(s) of the full suite, because of {causes}.\n"
            f"  full suite: {hist} · run it once with `dev test --all`, or `dev ci --run` before the push")
    if not json_out:
        print(f"{ui.WARN} {text}")
    return text


def _wide(top, st, runners, changed, reach, json_out) -> str:
    """The branch differs from its base in so many files that they reach a large part of the suite (`reach`, the test
    files a runner here takes): say so with the estimate, and name the tests of the files touched now (the uncommitted
    changes) to run instead."""
    _, label = base_ref(top)
    repo = _Repo(top)
    secs = []
    for r in runners:
        mine = [t for t in reach if owner(runners, t) is r]
        if mine:
            secs.append(estimate(st, r, mine, n_files=sum(1 for t in repo.tests if owner(runners, t) is r)))
    took = "" if not secs or None in secs else f" (~{_fmt(sum(secs))})"
    now = lane_files(top, changed_files(top, "HEAD"))[0]
    near = [t for t in (blast_radius(top, now)["tests"] if now else []) if owner(runners, t)]
    n = len(reach)
    if near and len(near) <= 8:
        mine = f"the tests of the files you touched now: dev test run {' '.join(near)}"
    elif near:
        mine = f"the {len(near)} test file(s) your uncommitted changes reach: dev test --since HEAD"
    else:
        mine = "the tests of the files you touched: dev test run <test files>"
    text = (f"not started: this branch differs from {label} in {len(changed):,} file(s), which reach {n} test "
            f"file(s){took}\n  {ui.ARROW} {mine}\n  {ui.ARROW} or compare with the commit your work started from: "
            f"dev test --since <ref> · all {n} of them: dev test --affected")
    if not json_out:
        print(f"{ui.WARN} {text}", file=sys.stderr)
    return text


def _flow(a, top, runners):
    """dev test / dev test run: failures first, then the affected tests; explicit paths; --all."""
    json_out, stream = a.json, (a.verbose or sys.stdout.isatty()) and not a.json
    st = load_state(top)
    data = {"project": project_key(top)[0], "top": top, "steps": [], "radius": None, "advice": None}
    code = 0

    def finish(rc):
        data["exit"] = rc
        if json_out:
            _emit(data)
        return rc

    paths = list(getattr(a, "paths", None) or [])
    if paths or a.all:
        if a.all:
            groups = [(r, {"all": True}) for r in runners]
            kind, header = "all", f"All tests · {len(groups)} runner(s)"
        else:
            groups, bad = _explicit(top, runners, paths)
            if bad:
                return _refuse(f"no test runner for: {', '.join(bad)}", "dev test plan shows the runners found here")
            kind, header = "paths", f"Selected tests · {len(paths)} path(s)"
        step = _run_step(top, groups, stream, json_out, kind, header)
        data["steps"].append(step)
        record(top, step["runs"], kind)
        zero = [r for r in step["runs"] if r["zero"]]
        if zero:
            _zero_problem(zero[0], paths)
            return finish(2)
        return finish(_verdict(step["runs"], json_out, kind))

    only_failed, only_affected = a.failed and not a.affected, a.affected and not a.failed
    if not only_affected:
        bad = _unloadable(top, st)
        if bad:
            _forget(top, set(bad))
            print(f"{ui.WARN} {len(bad)} recorded failure(s) name no test a runner can load: forgotten: "
                  f"{', '.join(i for _, _, i in bad[:5])}" + (f" and {len(bad) - 5} more" if len(bad) > 5 else ""),
                  file=sys.stderr)
        deleted = _deleted(top, st)
        if deleted:
            _forget(top, set(deleted))
        gone = [i for _, _, i in deleted]
        moved = _not_tests(top, st)
        if moved:
            _forget(top, set(moved))
            print(f"{ui.WARN} {len(moved)} recorded failure(s) are in a file that is no test here (outside the "
                  f"runner's test paths): forgotten: {', '.join(i for _, _, i in moved[:5])}"
                  + (f" and {len(moved) - 5} more" if len(moved) > 5 else ""), file=sys.stderr)
        groups = _failed_groups(top, runners, st)
        if not groups:
            _say_gone(gone)
        if not groups and only_failed:
            if not json_out:
                print(f"{ui.OK} no recorded failures for this work tree")
            return finish(0)
        if groups:
            n = sum(len(x[1]) for x in groups)
            step = _run_step(top, [(r, {"ids": fs}) for r, fs in groups], stream, json_out, "failed",
                             f"1 · Last failures · {n} test(s)")
            data["steps"].append(step)
            stale = [r for r in step["runs"] if r["zero"]]
            live = [r for r in step["runs"] if not r["zero"]]
            if stale:
                _forget(top, {(r["runner"], r["root"], i) for r in stale for i in r["ids"]})
            # a live run drops its missing names when it is recorded: they ran and did not fail
            gone += [i for r in stale for i in r["ids"]] + [i for r in live for i in r.get("not_found") or []]
            _say_gone(gone)
            if live:
                record(top, live, "failed")
            if any(r["failures"] for r in live):
                if not json_out:
                    n = sum(r["failed"] for r in live)
                    print(f"{ui.FAIL} {n} test(s) still fail: fix them, then `dev test` again (failures run first)")
                return finish(1)
            if only_failed:
                return finish(_verdict(live, json_out, "failed"))

    changed, outside = lane_files(top, changed_files(top, a.since))
    if outside:
        data["outside_lane"] = outside
        if not json_out:
            _outside_note(outside)
    radius = blast_radius(top, changed)
    data["radius"] = radius
    if radius["mode"] == "none" or (not radius["tests"] and radius["mode"] != "all"):
        if not json_out:
            why = "; ".join(f"{f}: {w}" for f, w in radius["reasons"][:4]) or "no changes"
            print(f"{ui.OK} nothing to run for the changes: {why}")
        return finish(code)
    groups, orphans = _group_tests(top, runners, radius["tests"], changed)
    reach = [t for t in radius["tests"] if t not in set(orphans)]
    plain = getattr(a, "test_cmd", None) is None and not (a.affected or a.since)  # `dev test run` starts what it selects
    if groups and plain and len(changed) >= WIDE_CHANGES and len(reach) >= WIDE_TESTS:
        data["advice"] = _wide(top, st, runners, changed, reach, json_out)
        return finish(2)
    if orphans and not json_out:
        print(f"{ui.WARN} no runner here for: {', '.join(orphans[:5])}", file=sys.stderr)
    results = []
    if groups:
        n = len(radius["tests"])
        step = _run_step(top, groups, stream, json_out, "affected",
                         f"{len(data['steps']) + 1} · Affected by {len(changed)} changed file(s) · {n} test file(s)")
        data["steps"].append(step)
        record(top, step["runs"], "affected")
        results = step["runs"]
        zero = [r for r in results if r["zero"]]
        if zero:
            _zero_problem(zero[0], changed)
            return finish(2)
    if radius["mode"] == "all":
        ran = [f for r in results for f in (r["files"] or [])] + \
            ([t for t in _Repo(top).tests for r in results if r["all"] and owner(runners, t) and
              owner(runners, t)["name"] == r["runner"] and owner(runners, t)["root"] == r["root"]])
        data["advice"] = _advice(st, radius, runners, json_out, top, ran)
    if results:
        return finish(_verdict(results, json_out, "affected", advised=radius["mode"] == "all"))
    return finish(code)


def _verdict(results, json_out, kind, advised=False) -> int:
    failed = sum(r["failed"] for r in results)
    count = sum(r["count"] for r in results)
    if failed:
        if not json_out:
            print(f"{ui.FAIL} {failed} of {count} test(s) failed · fix, then `dev test` (it runs these first)")
        return 1
    if not json_out:
        tail = "" if advised or kind == "all" else " · the full suite runs once before the push: dev ci --run"
        passed = sum(r["passed"] for r in results)
        outcomes = f"{passed} test(s) passed"
        for key, label in (("skipped", "skipped"), ("expected_failure", "expected failure(s)")):
            n = sum(r.get(key, 0) for r in results)
            if n:
                outcomes += f" · {n} {label}"
        if not passed:
            outcomes += " · no passing tests; verification is inconclusive"
        print(f"{ui.OK if passed else ui.WARN} {outcomes}{tail}")
    return 0


def _split_lists(paths) -> list:
    """The tests named, with a list passed as one word split into its words. zsh passes an unquoted $VAR whole, so
    `dev test run $MODULES` was one argument holding the list, which ran as one module name that does not exist and
    0 tests. A word with white space that names no file is such a list; white space inside the brackets of a pytest
    id's parameters (`test_add[one two]`) is part of that id."""
    out = []
    for raw in paths:
        words = _words_outside_brackets(raw)
        if len(words) > 1 and not os.path.exists(os.path.abspath(raw)):
            print(ui.dim(f"split one argument into {len(words)} tests (a list in one shell word: in zsh write "
                         f"${{=VAR}} or $(cat file) to pass it as words)"), file=sys.stderr)
            out += words
        else:
            out.append(raw)
    return out


def _words_outside_brackets(text) -> list:
    """`text` split at the white space outside square brackets. A [ that never closes holds no parameters: it is an
    ordinary character, so its word ends at the next white space."""
    # level[k]: the bracket depth before text[k], unclamped; a [ at k closes where the depth first comes back to
    # level[k], which is the next k' > k with the same level (it moves by one at a time)
    level = [0]
    for ch in text:
        level.append(level[-1] + (ch == "[") - (ch == "]"))
    closes, seen = [False] * len(text), set()
    for k in range(len(text), -1, -1):
        if k < len(text):
            closes[k] = text[k] == "[" and level[k] in seen
        seen.add(level[k])
    words, word, depth = [], "", 0
    for i, ch in enumerate(text):
        if ch.isspace() and depth == 0:
            if word:
                words.append(word)
            word = ""
            continue
        if depth or closes[i]:
            depth += (ch == "[") - (ch == "]")
        word += ch
    return words + ([word] if word else [])


def _explicit(top, runners, paths):
    """Paths (files, dirs, ids) relative to the cwd -> runner groups, plus what no runner takes: a path whose real path
    is outside the repository (`../x.test.js`, an absolute path, a symlink out of it) is no test of it."""
    groups, bad = {}, []
    tests = _Repo(top).tests
    real_top = os.path.realpath(top)
    for raw in _split_lists(paths):
        file_part, sep, rest = raw.partition("::")
        absf = os.path.abspath(file_part)
        real = os.path.realpath(absf)
        if real != real_top and not real.startswith(real_top.rstrip(os.sep) + os.sep):
            bad.append(raw)
            continue
        rel = os.path.relpath(absf, top).replace("\\", "/")
        if os.path.isdir(absf):
            picked = [t for t in tests if rel in (".", "") or t.startswith(rel.rstrip("/") + "/")]
            for t in picked:
                r = owner(runners, t)
                if r:
                    groups.setdefault(id(r), (r, {"files": [], "ids": []}))[1]["files"].append(t)
            if not picked:
                bad.append(raw)
            continue
        if os.path.exists(absf):
            r = owner(runners, rel)
            if r is None:
                bad.append(raw)
                continue
            g = groups.setdefault(id(r), (r, {"files": [], "ids": []}))[1]
            if sep:
                g["ids"].append(f"{os.path.relpath(absf, _rootdir(top, r))}::{rest}")
            else:
                g["files"].append(rel)
            continue
        if len(runners) == 1:  # a runner id: test_calc.CalcTests.test_add, com.acme.FooTest#adds, pkg:TestX
            groups.setdefault(id(runners[0]), (runners[0], {"files": [], "ids": []}))[1]["ids"].append(raw)
        else:
            bad.append(raw)
    return list(groups.values()), bad


# --------------------------------------------------------------------------- dev test plan


def _plan(a, top, runners) -> int:
    st = load_state(top)
    pid = project_key(top)[0]
    repo = _Repo(top)
    if a.all:
        rows = []
        for r in runners:
            mine = [t for t in repo.tests if owner(runners, t) is r]
            durs = (st.get("durations") or {}).get(f"{r['name']}:{r['root']}") or {}
            slow = sorted(((durs[t], t) for t in mine if t in durs), reverse=True)[:5]
            rows.append({"runner": r["name"], "root": r["root"], "files": len(mine),
                         "estimate": estimate(st, r, all_=True), "slowest": [[t, s] for s, t in slow],
                         "argv": build_command(top, r, all_=True)["argv"]})
        if a.json:
            print(json.dumps({"project": pid, "runners": rows}, indent=1))
            return 0
        print(ui.section(f"Test suite · {pid}"))
        for row in rows:
            print(f"  {row['runner']} ({row['root']}) · {row['files']} test file(s) · full run ~{_fmt(row['estimate'])}")
            print(ui.dim(f"    $ {core.tilde(' '.join(shlex.quote(x) for x in row['argv']))}"))
            for t, s in row["slowest"]:
                print(f"      {_fmt(s):>7}  {t}")
        if not rows:
            print("  no test runner found")
        return 0
    base, label = base_ref(top, a.since)
    changed, outside = lane_files(top, changed_files(top, a.since))
    if outside and not a.json:
        _outside_note(outside)
    radius = blast_radius(top, changed)
    failed = _failed_groups(top, runners, st)
    groups, orphans = _group_tests(top, runners, radius["tests"], changed)
    runs = []
    for r, sel in groups:
        n_files = sum(1 for t in repo.tests if owner(runners, t) is r)
        cmd = build_command(top, r, files=sel["files"], related=sel["related"], report="<report>",
                            all_=sel.get("all", False))
        runs.append({"runner": r["name"], "root": r["root"], "files": sel["files"], "related": sel["related"],
                     "all": sel.get("all", False), "argv": cmd["argv"], "source": r.get("source") or "inferred",
                     "declared_by": r.get("declared_by") or "",
                     "selection": next((x.get("selection") for x in repo.runners() if (x["name"], x["root"]) ==
                                        (r["name"], r["root"])), None) or "file names",
                     "estimate": estimate(st, r, all_=True) if sel.get("all") else
                     estimate(st, r, sel["files"], n_files=n_files)})
    ests = [x["estimate"] for x in runs]
    total = None if any(e is None for e in ests) else round(sum(ests), 1)
    full = [estimate(st, r, all_=True) for r in runners]
    full_total = None if not full or any(e is None for e in full) else round(sum(full), 1)
    if a.json:
        print(json.dumps({"project": pid, "base": label, "changed": changed, "radius": radius,
                          **({"outside_lane": outside} if outside else {}),
                          "failed": [f for _, fs in failed for f in fs], "runs": runs, "orphans": orphans,
                          "estimate": total, "full_estimate": full_total}, indent=1))
        return 0
    print(ui.section(f"Test plan · {pid} · {len(changed)} changed file(s) against {label}"))
    nf = sum(len(fs) for _, fs in failed)
    print(f"  last failures: {nf} test(s), run first" if nf else "  last failures: none")
    for r, fs in failed:
        for f in fs[:5]:
            print(ui.dim(f"    {r['name']} · {f['id']}"))
    by_file = radius.get("by_file") or {}
    groups_ = {}
    for f, why in radius["reasons"]:
        groups_.setdefault((why, tuple(by_file.get(f) or [])), []).append(f)
    for n, ((why, sel), fs) in enumerate(groups_.items()):
        if n == 15:
            print(ui.dim(f"  … {len(groups_) - 15} more reason(s): dev test plan --json"))
            break
        arrow = ", ".join(sel[:3]) + (f" +{len(sel) - 3}" if len(sel) > 3 else "") if sel else "-"
        more = f" (+{len(fs) - 1} more like it)" if len(fs) > 1 else ""
        print(f"  {fs[0]}{more}  {ui.ARROW} {arrow}  {ui.dim(why)}")
    for x in runs:
        what = "every test (the changes reach all of them)" if x["all"] else \
            f"related to {len(x['related'])} file(s)" if x["related"] else f"{len(x['files'])} file(s)"
        print(f"  would run: {x['runner']} · {what} · ~{_fmt(x['estimate'])}")
        print(ui.dim(f"    $ {core.tilde(' '.join(shlex.quote(y) for y in x['argv']))}"))
        if x["selection"] == "listed":
            print(ui.dim("    tests: as the runner's own listing says (dev test --listed)"))
        print(ui.dim(f"    command: declared by {x['declared_by']}" if x["source"] == "declared" else
                     "    command: inferred from the project's files (no test entry in its delivery policy, CI "
                     "workflow or package.json declares one)"))
    if orphans:
        print(f"  {ui.WARN} no runner for: {', '.join(orphans[:5])}")
    reach = [t for t in radius["tests"] if t not in set(orphans)]
    if runs and not a.since and len(changed) >= WIDE_CHANGES and len(reach) >= WIDE_TESTS:
        print(f"  {ui.WARN} {len(changed):,} changed files reach {len(reach)} test files: plain `dev test` does not "
              f"start them, and names the tests of the files you touched now · all of them: dev test --affected")
    if radius["mode"] == "all":
        print(f"  {ui.WARN} the changes can affect every test (see above); `dev test` runs the selection above "
              f"and says so · full suite ~{_fmt(full_total)}: dev test --all, or dev ci --run before the push")
    elif not runs:
        print("  nothing to run")
    else:
        print(f"  estimate: ~{_fmt(total)} · full suite: ~{_fmt(full_total)}")
    return 0


# --------------------------------------------------------------------------- dev test prove

_GO_UNIT = re.compile(r"^func (Test\w+)\s*\(", re.M)
_JVM_UNIT = re.compile(r"@(?:Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b[^\n]*\n(?:\s*@[^\n]*\n)*"
                       r"\s*(?:(?:public|private|protected|internal|open|override|suspend|static|final)\s+)*"
                       r"(?:fun\s+|void\s+|[\w<>\[\],]+\s+)?`?(\w+)`?\s*\(", re.M)
_PHP_UNIT = re.compile(r"(?:(?:@test|#\[Test\])[^\n]*\n(?:[^\n]*\n){0,3}?\s*(?:public\s+)?function\s+(\w+)"
                       r"|function\s+(test\w*)\s*\()", re.M)


_JS_CALL = re.compile(r"^[ \t]*(?:it|test)((?:\s*\.\s*\w+)*)\s*(?=[(`])", re.M)
_JS_TAKES_ARG = {"each", "skipIf", "runIf", "for"}  # it.each(table)(title, …), it.skipIf(cond)(title, …)
# members of test/it that are no test (Playwright's test.describe, test.use, test.step; vitest's test.extend)
_JS_NOT_TEST = {"describe", "use", "step", "extend", "configure", "beforeEach", "afterEach", "beforeAll", "afterAll",
                "info", "setTimeout", "slow", "expect", "scoped", "override"}
_JS_HOOK = re.compile(r"^[ \t]*(?:beforeEach|afterEach|beforeAll|afterAll)\s*\(", re.M)
_JS_DECL = re.compile(r"^[ \t]*(?:export\s+(?:default\s+)?)?(?:(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"
                      r"|class\s+([A-Za-z_$][\w$]*)|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=)", re.M)
_JS_REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^") | {""}


def _js_mask(text: str) -> str:
    """`text` with the insides of its strings, template literals, regular expressions and comments blanked (new lines
    kept): brackets and keywords found in the result are code."""
    out, i, n, last = list(text), 0, len(text), ""

    def blank(a, b):
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "
    while i < n:
        c = text[i]
        if c in "'\"":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            blank(i + 1, j)
            i, last = j + 1, c
        elif c == "`":
            j, depth = i + 1, 0
            while j < n and (text[j] != "`" or depth):
                if text[j] == "\\":
                    j += 2
                    continue
                if text.startswith("${", j):
                    depth, j = depth + 1, j + 2
                    continue
                if text[j] == "}" and depth:
                    depth -= 1
                j += 1
            blank(i + 1, j)
            i, last = j + 1, c
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif c == "/" and last in _JS_REGEX_BEFORE:
            j, cls = i + 1, False
            while j < n and text[j] != "\n" and (text[j] != "/" or cls):
                if text[j] == "\\":
                    j += 1
                elif text[j] == "[":
                    cls = True
                elif text[j] == "]":
                    cls = False
                j += 1
            blank(i + 1, j)
            i, last = j + 1, "/"
        else:
            if not c.isspace():
                last = c if not (c.isalnum() or c in "_$") else "a"
            i += 1
    return "".join(out)


def _js_close(masked: str, at: int) -> int:
    """The index of the bracket closing the one at `at` in masked text (_js_mask), or the end of the text."""
    pairs, stack = {"(": ")", "[": "]", "{": "}"}, []
    for k in range(at, len(masked)):
        c = masked[k]
        if c in pairs:
            stack.append(pairs[c])
        elif c in ")]}" and stack:
            if stack.pop() != c:
                return k
            if not stack:
                return k
    return len(masked) - 1


def _js_statement_end(masked: str, at: int) -> int:
    """The index where the statement starting at `at` ends: a new line outside brackets after a complete expression."""
    depth, k = 0, at
    while k < len(masked):
        c = masked[k]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth < 0:
                return k - 1
        elif c == ";" and depth == 0:
            return k
        elif c == "\n" and depth == 0:
            before = masked[at:k].rstrip()
            after = masked[k:].lstrip()
            if before and not before.endswith(("=", ",", "(", "[", "{", "+", "-", "*", "/", "%", "&", "|", "?",
                                               ":", "<", ">", "!", ".", "=>")) and \
                    not after.startswith((".", "?", ":", "+", "-", "*", "/", "&&", "||", "=>")):
                return k - 1
        k += 1
    return len(masked) - 1


def _line_of(text: str, i: int) -> int:
    return text.count("\n", 0, max(0, i)) + 1


def _js_units(text: str) -> list:
    """JS/TS tests: [(title, first line, last line)], each to the bracket closing its it()/test() call, so the code
    between two tests (a helper, a describe) belongs to neither. it.each(table)(title) and it.each`table`(title)
    are a unit named by their title template (%i, $a), whose cases _member matches."""
    masked, out = _js_mask(text), []
    for m in _JS_CALL.finditer(masked):
        mods = re.findall(r"\w+", m.group(1))
        if _JS_NOT_TEST & set(mods):
            continue
        k = m.end()
        if mods and mods[-1] in _JS_TAKES_ARG:
            if masked[k] == "`":
                k = masked.find("`", k + 1) + 1
            else:
                k = _js_close(masked, k) + 1
            while k < len(masked) and masked[k].isspace():
                k += 1
            if k <= 0 or k >= len(masked) or masked[k] != "(":
                continue
        if masked[k] != "(":
            continue
        q = k + 1
        while q < len(text) and text[q].isspace():
            q += 1
        if q >= len(text) or text[q] not in "'\"`":
            continue
        close = text.find(text[q], q + 1)
        while close > 0 and text[close - 1] == "\\":
            close = text.find(text[q], close + 1)
        if close < 0:
            continue
        end = _js_close(masked, k)
        out.append((text[q + 1:close], _line_of(text, m.start() + len(m.group(0)) - len(m.group(0).lstrip())),
                    _line_of(text, end)))
    return out


def _js_support(text: str) -> list:
    """The test support of a JS/TS test file outside its tests: [(name or '', first line, last line)] of its named
    helpers (function, class, const/let/var) and, named '', its hooks (beforeEach, afterAll, …)."""
    masked = _js_mask(text)
    tests = [(s, e) for _, s, e in _js_units(text)]
    out = []
    for m in _JS_DECL.finditer(masked):
        start = _line_of(text, m.start())
        if any(s <= start <= e for s, e in tests):
            continue
        name = next(g for g in m.groups() if g)
        if m.group(3):
            end = _js_statement_end(masked, m.end())
        else:
            brace = masked.find("{", m.end())
            end = _js_close(masked, brace) if brace >= 0 else m.end()
        out.append((name, start, _line_of(text, end)))
    for m in _JS_HOOK.finditer(masked):
        start = _line_of(text, m.start())
        if not any(s <= start <= e for s, e in tests):
            out.append(("", start, _line_of(text, _js_close(masked, m.end() - 1))))
    return out


# a class member's head: its modifiers, then its name (a word, #private, a [computed] key or a quoted one)
_JS_MEMBER = re.compile(r"(?:(?:static|async|get|set|public|private|protected|readonly|override|abstract|declare|"
                        r"accessor)\s+)*\*?\s*(#?[A-Za-z_$][\w$]*|\[|['\"])")
_JS_METHOD = re.compile(r"\s*[?!]?\s*(?:<[^>\n]*>)?\s*\(")  # after a member's name: it is a method
_JS_DECORATOR = re.compile(r"@[\w$.]+")


def _js_class_members(text: str, masked: str, brace: int) -> list | None:
    """The members of the class whose body opens at `brace`, read at the class body's own depth only, so nothing
    inside a member's body (a call in an arrow property, an if block) is one: [(name, first line, last line)], a
    member's decorators included. None when the body holds something no member starts with."""
    close, out, k = _js_close(masked, brace), [], brace + 1
    while True:
        while k < close and (masked[k].isspace() or masked[k] == ";"):
            k += 1
        if k >= close:
            return out
        start = k
        while k < close and masked[k] == "@":  # decorators, each with its arguments
            d = _JS_DECORATOR.match(masked, k)
            k = d.end() if d else k + 1
            if masked[k:k + 1] == "(":
                k = _js_close(masked, k) + 1
            while k < close and masked[k].isspace():
                k += 1
        m = _JS_MEMBER.match(masked, k, close)
        if not m:
            return None
        at, name = m.start(1), m.group(1)
        if name == "[":
            end = _js_close(masked, at)
            name, after = text[at:end + 1], end + 1
        elif name in "'\"":
            end = masked.find(name, at + 1, close)
            name, after = text[at:end + 1], end + 1
        else:
            after = m.end()
        method = _JS_METHOD.match(masked, after, close)
        if method:  # a method: to the end of its body (or of a declaration without one)
            params = _js_close(masked, method.end() - 1)
            body = next((i for i in range(params + 1, close) if masked[i] in "{;"), close - 1)
            end = _js_close(masked, body) if masked[body] == "{" else body
        else:  # a field, a static block: to the end of its statement
            end = _js_statement_end(masked, k)
        out.append((name, _line_of(text, start), _line_of(text, end)))
        k = max(end + 1, k + 1)


def _js_classes(text: str) -> list:
    """The classes outside the tests of a JS/TS test file: [(name, first line, last line, members (see
    _js_class_members))]."""
    masked = _js_mask(text)
    tests = [(s, e) for _, s, e in _js_units(text)]
    out = []
    for m in _JS_DECL.finditer(masked):
        if not m.group(2) or any(s <= _line_of(text, m.start()) <= e for s, e in tests):
            continue
        brace = masked.find("{", m.end())
        if brace >= 0:
            out.append((m.group(2), _line_of(text, m.start()), _line_of(text, _js_close(masked, brace)),
                        _js_class_members(text, masked, brace)))
    return out


def _js_members(text: str) -> dict:
    """The members (methods, fields) of the classes outside the tests of a JS/TS test file: {(class, member): (first
    line, last line)}."""
    return {(c, x): (s, e) for c, _, _, members in _js_classes(text) for x, s, e in members or ()}


def _py_members(text) -> dict:
    """The members (methods, attributes) of the classes without tests of a Python test module: {(class, name):
    (first line, last line)}."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    fn = (ast.FunctionDef, ast.AsyncFunctionDef)
    out, plain = {}, set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        mine = [i for i in node.body if isinstance(i, fn)]
        if _py_test_class(node, plain) and any(i.name.startswith("test") for i in mine):
            continue
        for i in node.body:
            if isinstance(i, (ast.Assign, ast.AnnAssign)):
                names = [t.id for t in (i.targets if isinstance(i, ast.Assign) else [i.target])
                         if isinstance(t, ast.Name)]
            else:
                names = [i.name] if isinstance(i, fn + (ast.ClassDef,)) else []
            first = min([i.lineno] + [d.lineno for d in getattr(i, "decorator_list", [])])
            out.update({(node.name, x): (first, getattr(i, "end_lineno", i.lineno)) for x in names})
    return out


def _py_test_class(node, plain) -> bool:
    """Whether a runner can collect the tests of class `node` of a Python test module. unittest collects a TestCase,
    which always has a base; pytest a class named Test… (or …Test, …Tests where a project sets python_classes). A
    plain class (no base, or only object and the module's other plain classes) named otherwise is a fake or a helper
    whose test_… methods no runner collects. `plain`: the module's plain classes so far, which this one joins."""
    bases = [b.id if isinstance(b, ast.Name) else "" for b in node.bases]
    if all(b == "object" or b in plain for b in bases) and not node.keywords:
        if not (node.name.startswith("Test") or node.name.endswith(("Test", "Tests"))):
            plain.add(node.name)
            return False
    return True


def _units(f, text) -> list:
    """Test units of a test file: [(name, first line, last line)]; Python classes add ('Class.*', …) for setUp."""
    lang = _lang(f)
    if lang == "py":
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return []
        fn = (ast.FunctionDef, ast.AsyncFunctionDef)

        def span(n):
            return min([n.lineno] + [d.lineno for d in n.decorator_list]), getattr(n, "end_lineno", n.lineno)

        out, plain = [], set()
        for node in tree.body:
            if isinstance(node, fn) and node.name.startswith("test"):
                out.append((node.name,) + span(node))
            elif isinstance(node, ast.ClassDef) and _py_test_class(node, plain):
                mine = [(f"{node.name}.{i.name}",) + span(i) for i in node.body
                        if isinstance(i, fn) and i.name.startswith("test")]
                if mine:
                    out += mine + [(f"{node.name}.*",) + span(node)]
        return out
    if lang == "js":
        return _js_units(text)
    rx = {"go": _GO_UNIT, "jvm": _JVM_UNIT, "php": _PHP_UNIT}.get(lang)
    if rx is None:
        return []
    starts = []
    for m in rx.finditer(text):
        name = next((g for g in m.groups() if g), None)
        if name:
            starts.append((name, text.count("\n", 0, m.start()) + 1))
    total = text.count("\n") + 1
    return [(n, s, (starts[i + 1][1] - 1) if i + 1 < len(starts) else total) for i, (n, s) in enumerate(starts)]


def _changed_lines(top, base, f) -> set:
    out = _git(top, "diff", "-U0", "--no-color", "--no-ext-diff", base, "--", f)
    lines = set()
    for m in re.finditer(r"(?m)^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", out):
        start, n = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        lines |= set(range(start, start + n)) if n else {start, start + 1}
    return lines


def _py_named(node) -> str | None:
    """The name a Python statement gives as a test unit or helper: a function's or a class's own, or an assignment's
    targets (joined by commas); None for any other statement."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return ",".join(x.id for t in targets for x in ast.walk(t) if isinstance(x, ast.Name)) or None
    return None


def _py_entries(text) -> list | None:
    """A Python test module statement by statement, as the run without the change restores it: [(key, first line,
    last line, kind, node)] in the order of the text, each with its decorators. key: (class or '', name, occurrence),
    an assignment named by its targets. kind: "unit", a helper restored or kept whole (a module-level function that is
    no test, an assignment, a member of a test class that is no test, a member of a class without tests); "class", a
    class without tests, whose members are units and whose lines before them are its "head"; "tests", a test class;
    "fixed", a test. A class and a test class only hold their members, and a test keeps the change: they place a
    helper the change removed. Imports and other statements are none of these. None when the text does not parse."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    fn = (ast.FunctionDef, ast.AsyncFunctionDef)
    out, seen, plain = [], {}, set()

    def span(n):
        return min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])]), getattr(n, "end_lineno", n.lineno)

    def add(cls, name, s, e, kind, node):
        k = seen[(cls, name)] = seen.get((cls, name), -1) + 1
        out.append(((cls, name, k), s, e, kind, node))

    for node in tree.body:
        name = _py_named(node)
        if isinstance(node, ast.ClassDef):
            tests = [i for i in node.body if isinstance(i, fn) and i.name.startswith("test")]
            if _py_test_class(node, plain) and tests:
                add("", name, *span(node), "tests", node)
                for i in node.body:
                    if _py_named(i):
                        add(name, _py_named(i), *span(i), "fixed" if i in tests else "unit", i)
            elif span(node.body[0])[0] > node.lineno:
                add("", name, *span(node), "class", node)
                add(name, "<head>", span(node)[0], span(node.body[0])[0] - 1, "head", node)
                for i in node.body:
                    add(name, _py_named(i) or "<statement>", *span(i), "unit", i)
            else:  # a class on one line is one unit
                add("", name, *span(node), "unit", node)
        elif name:
            add("", name, *span(node), "fixed" if isinstance(node, fn) and name.startswith("test") else "unit", node)
    return out


def _js_entries(text) -> list:
    """A JS/TS test module as the run without the change restores it (see _py_entries; node is None): its tests, its
    declarations and hooks outside them (one inside another's body is part of it, and one that holds a test is none),
    and the members of its classes."""
    masked, tests, cands = _js_mask(text), _js_units(text), []
    cands += [(s, e, "fixed", title, None) for title, s, e in tests]
    for m in _JS_DECL.finditer(masked):
        start = _line_of(text, m.start())
        if any(s <= start <= e for _, s, e in tests):
            continue
        members = None
        if m.group(3):
            end = _js_statement_end(masked, m.end())
        else:
            brace = masked.find("{", m.end())
            if brace < 0:
                continue
            end = _js_close(masked, brace)
            members = _js_class_members(text, masked, brace) if m.group(2) else None
        kind = "class" if members and members[0][1] > start else "unit"
        cands.append((start, _line_of(text, end), kind, next(g for g in m.groups() if g), members))
    for m in _JS_HOOK.finditer(masked):
        start = _line_of(text, m.start())
        if not any(s <= start <= e for _, s, e in tests):
            cands.append((start, _line_of(text, _js_close(masked, m.end() - 1)), "unit", m.group(0).strip(" \t("),
                          None))
    out, seen, held = [], {}, []

    def add(cls, name, s, e, kind):
        k = seen[(cls, name)] = seen.get((cls, name), -1) + 1
        out.append(((cls, name, k), s, e, kind, None))
    for s, e, kind, name, members in sorted(cands, key=lambda c: (c[0], -c[1])):
        if kind != "fixed" and any(s <= ts <= e for _, ts, _ in tests) or any(a <= s <= b for a, b in held):
            continue
        held.append((s, e))
        add("", name, s, e, kind)
        if kind == "class":
            add(name, "<head>", s, members[0][1] - 1, "head")
            for x, ms, me in members:
                add(name, x, ms, me, "unit")
    return out


_LEAF = ("unit", "head")  # the entries a restore compares and rebuilds; a class holds them, a test keeps the change


def _same_shape(old, now) -> bool:
    """Whether the entries of one class and name, [(kind, body)] in order at the base (`old`) and now (`now`), keep
    their shape: the same kinds in the same places (a getter beside its setter, a beforeEach in each of two
    describes), and no helper whose text now is the base text of another in another place (helpers that traded
    places). Each place then comes back as its own base text."""
    if [kind for kind, _ in old] != [kind for kind, _ in now]:
        return False
    helpers = [i for i, (kind, _) in enumerate(old) if kind not in ("fixed", "tests")]
    return not any(old[i][1] != now[i][1] and any(j != i and now[i][1] == old[j][1] != old[i][1] for j in helpers)
                   for i in helpers)


def _rebuild_helpers(f, old_text, text) -> dict | None:
    """Test module `f` (`text`, its text now) with its changed test support rebuilt from `old_text`, its text at the
    base, helper by helper (_py_entries, _js_entries), never from where git's diff puts a changed line: a helper whose
    text changed comes back as its own base text, one the change removed comes back next to the helper before it at
    the base (or after it), and one the change adds stays whole, as an added helper file does; the tests, the imports
    and every other line keep the change. The result is checked: read again, each helper undone must be its base text
    and each other one its text now, and no import the helpers undone read and no statement outside the tests,
    helpers and imports that names a name they read (_scope, _undone_reads) may have changed, else no restore is made.
    Nor is one when the places of a class and name shift: their kinds change places on the two sides (_same_shape), a
    side has more than one helper of that name, and the change adds, removes, moves or edits one; or a test of that
    name takes the place of the one helper of that name, which the change edits or removes.
    -> None when nothing is to be undone (or a side does not parse), else {"text": the module, None when the check
    failed; "old", "now": the entries at the base and now; "undone": the keys of the helpers rebuilt or put back;
    "added": the keys of the helpers the change adds; "lines": the lines at the base and now; "scope": the words of
    each statement outside the tests, helpers and imports that the change adds, edits or moves}."""
    ou = _py_entries(old_text) if _lang(f) == "py" else _js_entries(old_text)
    nu = _py_entries(text) if _lang(f) == "py" else _js_entries(text)
    if ou is None or nu is None:
        return None
    # every line ends with its new line, so a helper put back after the last line of the text stays on lines of its own
    old, cur = ([x if x.endswith("\n") else x + "\n" for x in t.splitlines(keepends=True)] for t in (old_text, text))
    new = list(cur)
    ob, nb = {k: x for k, *x in ou}, {k: x for k, *x in nu}
    edits, undone, added, clean = [], [], [], True  # edits: (index in `new`, lines replaced, their base text)
    for k, s, e, kind, _ in nu:
        if kind not in _LEAF:
            continue
        if k not in ob or ob[k][2] not in _LEAF:
            added.append(k)
        elif _body(old, *ob[k][:2]) != _body(new, s, e):
            edits.append((s - 1, e - s + 1, old[ob[k][0] - 1:ob[k][1]]))
            undone.append(k)
    for i, (k, s, e, kind, _) in enumerate(ou):
        # a helper the change removed (a whole class without tests too) goes back among its own class's members
        if k in nb or kind not in ("unit", "class") or k[0] and ("", k[0], 0) not in nb:
            continue
        before = [x for x, *_ in ou[:i] if x[0] == k[0] and x in nb]
        after = [x for x, *_ in ou[i + 1:] if x[0] == k[0] and x in nb]
        if not before and not after:
            clean = False
            continue
        edits.append((nb[before[-1]][1] if before else nb[after[0]][0] - 1, 0, old[s - 1:e]))
        undone.append(k)
    # entries of one class and name, tests too, are matched by their place among them. A test of that name that takes
    # the place of the one helper of that name pairs the helper with the test's base text: the helper reads as added
    # or as gone, and its edit would not be undone. And where a side has more than one helper of that name, one the
    # change adds, removes, moves or edits before others pairs them with their neighbors' base text. Neither can be
    # rebuilt; more than one helper of a name only matters when some helper is rebuilt
    groups, crowded, displaced = [{}, {}], False, False
    for g, entries, lines in ((groups[0], ou, old), (groups[1], nu, cur)):
        for (c, n, _), s, e, kind, _ in entries:
            g.setdefault((c, n), []).append((kind, _body(lines, s, e)))
    for key in groups[0].keys() | groups[1].keys():
        o, n = groups[0].get(key, []), groups[1].get(key, [])
        ho, hn = ([(i, body) for i, (kind, body) in enumerate(x) if kind not in ("fixed", "tests")] for x in (o, n))
        if ho == hn or _same_shape(o, n):
            continue
        if len(ho) > 1 or len(hn) > 1:
            crowded = True
        elif ho and (hn and ho[0][0] != hn[0][0] and ho[0][1] != hn[0][1] or not hn and ho[0][0] < len(n)):
            displaced = True  # the one helper of that name changed or went, and a test took its place
    if not edits and not displaced:
        return None
    # from the end, so each index still points where it did; at one index a replacement goes before an insertion,
    # and insertions keep their order at the base
    for at, count, lines in [x for _, x in sorted(((x[0], x[1] > 0, n), x) for n, x in enumerate(edits))][::-1]:
        new[at:at + count] = lines
    text_out = "".join(new)
    got = _py_entries(text_out) if _lang(f) == "py" else _js_entries(text_out)
    want = {k: _body(old, *ob[k][:2]) if k in undone else _body(cur, s, e) for k, s, e, kind, _ in nu
            if kind not in ("class", "tests")}
    for k in undone:  # put back: a helper, or a whole class without tests, part by part
        whole = ob[k][2] == "class"
        want.update({x: _body(old, a, b) for x, a, b, kind, _ in ou if (x == k or whole and x[0] == k[1]) and
                     kind not in ("class", "tests")})
    have = {k: _body(new, s, e) for k, s, e, kind, _ in got or () if kind not in ("class", "tests")}
    ok = clean and not crowded and not displaced and got is not None and have == want
    if ok:  # imports keep the change: a helper back as its base text must still find each name an import gave it
        py = _lang(f) == "py"
        gone = (_py_imported if py else _js_imported)(old_text) - (_py_imported if py else _js_imported)(text_out)
        ok = not gone & {x for k in undone for x in (_py_names(ob[k][3], ob[k][2]) if py else
                                                      _js_names("".join(old[ob[k][0] - 1:ob[k][1]])))}
    # and no statement outside the tests, helpers and imports that the change adds, removes, edits or moves may name
    # a name a rebuilt helper reads: such a statement keeps the change, so the helper would run in another scope than
    # at the base (a value it read gone, a name deleted, a registry left empty), whatever binds that name now
    gone, came = _scope_changes(_scope(f, old_text, ou), _scope(f, text, nu))
    ok = ok and not _undone_reads(f, ou, undone, old) & {w for _, words in gone + came for w in words}
    return {"text": text_out if ok else None, "old": ou, "now": nu, "undone": undone, "added": added,
            "lines": (old, cur), "scope": [words for _, words in came]}


def _helper_restore(top, base, f) -> dict | None:
    """_rebuild_helpers for test module `f` from its text at `base`; None for a module new since `base`, or neither
    Python nor JavaScript."""
    if _lang(f) not in ("py", "js"):
        return None
    p = subprocess.run(["git", "-C", top, "cat-file", "blob", f"{base}:{f}"], capture_output=True, env=core.GIT_ENV)
    if p.returncode != 0:
        return None
    return _rebuild_helpers(f, p.stdout.decode("utf-8", "replace"), _read(os.path.join(top, f)))


def _support_text(top, base, f):
    """Test module `f` with its changed test support restored from `base` (_rebuild_helpers): a fix in a helper
    defined inside a test module is part of the change, as a fix in a helper file is, so the module's tests run
    against the helper's base text without the change. -> the text, None when no helper of `f` changed, or
    _SUPPORT_TANGLED when the helpers do not come back as their base text."""
    r = _helper_restore(top, base, f)
    return None if r is None else _SUPPORT_TANGLED if r["text"] is None else r["text"]


# _support_text's answer for a test module whose helpers do not come back helper by helper as their base text
_SUPPORT_TANGLED = object()


def _py_imported(text) -> set:
    """The names a Python module's imports bind outside its functions and classes (in if and try blocks too)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            todo = list(ast.parse(text).body)
    except (SyntaxError, ValueError):
        return set()
    out = set()
    while todo:
        n = todo.pop()
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            out |= {(a.asname or a.name).split(".")[0] for a in n.names}
        elif not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            todo += ast.iter_child_nodes(n)
    return out


def _js_imported(text) -> set:
    """The names a JS/TS module's import statements bind."""
    return {w for m in re.finditer(r"(?m)^[ \t]*import\s+([\w$\s{},*]+?)\s+from\b", _js_mask(text))
            for w in re.findall(r"[A-Za-z_$][\w$]*", m.group(1)) if w not in ("as", "type")}


_JS_KEYWORDS = {"abstract", "as", "async", "await", "break", "case", "catch", "class", "const", "continue",
                "debugger", "declare", "default", "delete", "do", "else", "export", "extends", "false", "finally",
                "for", "from", "function", "get", "if", "implements", "import", "in", "instanceof", "interface", "let",
                "new", "null", "of", "private", "protected", "public", "readonly", "return", "set", "static", "super",
                "switch", "this", "throw", "true", "try", "type", "typeof", "undefined", "var", "void", "while",
                "with", "yield"}
_PY_BUILTINS = set(dir(builtins))


def _py_names(node, kind="unit", outside=True) -> set:
    """The names a Python helper or test reads or sets, never those in its strings or comments: its names (Python's
    built-in ones left out), attributes, parameters and keyword arguments; of a class's head, those of its decorators,
    bases and keywords. outside: of a function, only those a caller can see: its attributes and the names it neither
    takes nor binds."""
    fn = (ast.FunctionDef, ast.AsyncFunctionDef)
    parts = node.decorator_list + node.bases + node.keywords if kind == "head" else [node]
    attrs, ids, bound, free = set(), set(), set(), set()
    for n in (x for p in parts for x in ast.walk(p)):
        if isinstance(n, ast.Attribute):
            attrs.add(n.attr)
        elif isinstance(n, ast.Name):
            ids.add(n.id)
            if not isinstance(n.ctx, ast.Load):
                bound.add(n.id)
        elif isinstance(n, ast.arg) or isinstance(n, fn + (ast.ClassDef,)) and n is not node or \
                isinstance(n, ast.ExceptHandler) and n.name:
            ids.add(getattr(n, "arg", None) or n.name)
            bound.add(getattr(n, "arg", None) or n.name)
        elif isinstance(n, ast.alias):
            ids.add((n.asname or n.name).split(".")[0])
            bound.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, ast.keyword) and n.arg:
            ids.add(n.arg)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            free.update(n.names)
    if outside and kind != "head" and isinstance(node, fn):
        ids -= bound - free
    return attrs | (ids - _PY_BUILTINS)


def _js_names(text) -> set:
    """The names in JS/TS code, never those in its strings or comments, its keywords left out."""
    return set(re.findall(r"[A-Za-z_$][\w$]*", _js_mask(text))) - _JS_KEYWORDS


def _js_params(text) -> str:
    """The text inside the first brackets of JS/TS code (a function's or a method's parameters), spaces left out."""
    masked = _js_mask(text)
    i = masked.find("(")
    return re.sub(r"\s+", "", masked[i:_js_close(masked, i) + 1]) if i >= 0 else ""


def _py_gives(node, kind="unit") -> set:
    """The names a Python helper gives the code around it: its own name or the names it assigns, a class's members, and
    the attributes it sets and the globals it declares anywhere inside it."""
    if kind == "head":
        return {node.name}
    out = set()
    for n in [node] + (node.body if isinstance(node, ast.ClassDef) else []):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            out |= {x.id for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) for x in ast.walk(t)
                    if isinstance(x, ast.Name)}
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and not isinstance(n.ctx, ast.Load):
            out.add(n.attr)
        elif isinstance(n, ast.Global):
            out.update(n.names)
    return out


def _py_free(node, kind="unit") -> set:
    """The names a Python helper reads that it does not bind itself (_py_names); of a whole class, those of its head and
    of each member."""
    if kind == "class":
        return _py_names(node, "head") | {x for i in node.body for x in _py_names(i)}
    return _py_names(node, kind)


def _js_local(text) -> set:
    """The names JS/TS code declares for itself: after const, let, var, function or class, in a destructuring
    declaration, as the parameters of a function, a method or an arrow, and as a caught error."""
    # each pattern starts at a word's first character and stays within a line or a bracket, so it reads in linear time
    masked, out = _js_mask(text), set()
    out |= set(re.findall(r"\b(?:const|let|var|function\s*\*?|class)\s+([A-Za-z_$][\w$]*)", masked))
    out |= set(re.findall(r"(?<![\w$])([A-Za-z_$][\w$]*)[ \t]*=>", masked))
    for group in (re.findall(r"\b(?:const|let|var)\s*([{\[][^=;{}\[\]]*[}\]])\s*=", masked) +
                  re.findall(r"\(([^()]*)\)[ \t]*(?::[^=;{}()\n]+)?=>", masked) +
                  re.findall(r"\bcatch\s*\(([^()]*)\)", masked) +
                  re.findall(r"(?:\bfunction\b[ \t]*\*?[ \t]*[\w$]*|^[ \t]*(?!(?:if|for|while|switch|catch|with|return)\b)"
                             r"(?:(?:static|async|get|set|public|private|protected|readonly)[ \t]+)*\*?[A-Za-z_$][\w$]*)"
                             r"[ \t]*\(([^()]*)\)[ \t]*(?::[^{;=\n]+)?\s*\{", masked, re.M)):
        out |= set(re.findall(r"[A-Za-z_$][\w$]*", group))
    return out


def _js_gives(text) -> set:
    """The properties JS/TS code sets (`this.n = …`, `globalThis.LIMIT = …`)."""
    return set(re.findall(r"\.\s*([A-Za-z_$][\w$]*)\s*=(?![=>])", _js_mask(text)))


_PY_WORD = re.compile(r"[^\W\d]\w*")
_JS_WORD = re.compile(r"[A-Za-z_$][\w$]*")
_JS_IMPORT_DECL = re.compile(r"^[ \t]*import\b(?![ \t]*[(.])", re.M)  # import declarations, no import() or import.meta
# before a string, a call that makes it a describe's, an it's or a test's title: words for people, no names
_JS_TITLED = re.compile(r"(?<![\w$.])(?:describe|suite|context|it|test|bench)(?:\s*\.\s*[\w$]+)*\s*\(\s*$")


def _py_words(node) -> set:
    """Every word Python code names: its names, attributes, parameters, keyword arguments and imports, the names it
    defines or declares, and the words of its strings (a name set by its text, as in setattr(NS, 'limit', 1)), those
    of a bare string (a docstring) left out."""
    doc = node.value if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) else None
    out = set()
    for n in ast.walk(node):
        if n is doc:
            continue
        for field, value in ast.iter_fields(n):
            if field not in ("kind", "type_comment"):
                out.update(w for x in (value if isinstance(value, list) else [value]) if isinstance(x, str)
                           for w in _PY_WORD.findall(x))
    return out


def _js_statement(raw, masked, numbers) -> tuple:
    """(the JS/TS statement on lines `numbers` normalized, the words it names) for _scope: `raw`, `masked` (_js_mask),
    the module's lines."""
    code, text = ("\n".join(x[i - 1] for i in numbers) for x in (masked, raw))
    words, strings = set(_JS_WORD.findall(code)) - _JS_KEYWORDS, []
    for m in re.finditer(r"(['\"`])([ \n]*)\1", code):  # a string: its text blanked between its quotes
        strings.append(text[m.start(2):m.end(2)])
        if not _JS_TITLED.search(code, max(0, m.start() - 200), m.start()):
            words.update(_JS_WORD.findall(strings[-1]))
    return (tuple(re.sub(r"\s+", " ", masked[i - 1].strip()) for i in numbers), tuple(strings)), words


def _scope(f, text, entries) -> list:
    """The code of test module `f` (`text`; `entries`: _py_entries or _js_entries) outside its tests, helpers and
    imports, statement by statement in order: [(the statement normalized, the words it names)]. Python: each
    module-level statement that is none of those (an if, try, for, with or match block, a call, a del); JS/TS: each
    statement on the lines outside them, one over several lines whole, comment-only lines left out. The words: its
    names and the words of its strings (_py_words), a JS/TS describe's, it's or test's title left out."""
    if _lang(f) == "py":
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return []
        return [(ast.dump(n), _py_words(n)) for n in tree.body
                if not isinstance(n, (ast.Import, ast.ImportFrom)) and _py_named(n) is None]
    masked = _js_mask(text)
    raw, lines = text.split("\n"), masked.split("\n")
    held = {i for _, s, e, _, _ in entries for i in range(s, e + 1)}
    for m in _JS_IMPORT_DECL.finditer(masked):
        held.update(range(_line_of(text, m.start()), _line_of(text, _js_statement_end(masked, m.end())) + 1))
    out, part, depth = [], [], 0
    for i, line in enumerate(lines, 1):
        if i in held or not line.strip():
            continue
        part.append(i)  # a statement ends on a line where its brackets close (one opened inside an entry: anywhere)
        depth = max(0, depth + sum(map(line.count, "([{")) - sum(map(line.count, ")]}")))
        if not depth:
            out.append(_js_statement(raw, lines, part))
            part = []
    return out + ([_js_statement(raw, lines, part)] if part else [])


def _scope_changes(old, now) -> tuple:
    """(the statements of `old` that the change removed, edited or moved, those of `now` that it added, edited or
    moved): of two _scope lists, the statements they do not share in order."""
    gone, came = [], []
    matcher = difflib.SequenceMatcher(None, [x for x, _ in old], [x for x, _ in now], autojunk=False)
    for tag, a, b, c, d in matcher.get_opcodes():
        if tag != "equal":
            gone += old[a:b]
            came += now[c:d]
    return gone, came


def _undone_reads(f, entries, undone, lines) -> set:
    """The names the helpers `undone` of test module `f` read as their base text (`entries`, `lines`: the module's at
    the base), those they give each other left out: their free names, the attributes and properties they read."""
    py = _lang(f) == "py"
    ob = {k: x for k, *x in entries}
    need, gives = set(), set()
    for k in undone:
        s, e, kind, node = ob[k]
        if py:
            need |= _py_free(node, kind)
            gives |= _py_gives(node, kind)
        else:
            text = "".join(lines[s - 1:e])
            need |= _js_names(text) - _js_local(text)
            gives |= _js_gives(text) | {k[0], k[1]} | ({x[1] for x in ob if x[0] == k[1]} if kind == "class" else set())
    return need - gives


_PY_DYNAMIC = {"setattr", "exec", "eval", "globals", "vars", "locals", "namedtuple", "make_dataclass", "new_class",
               "NamedTuple", "TypedDict", "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "SimpleNamespace"}
_JS_DYNAMIC = re.compile(r"\bObject\s*\.\s*(?:assign|defineProperty|defineProperties|setPrototypeOf|create|"
                         r"fromEntries)\b|\bReflect\s*\.|\beval\s*\(|\bnew\s+Function\b|\]\s*=(?![=>])|"
                         r"(?:[=(,:?\[]|=>|\breturn)\s*class\b")


def _py_dynamic(node) -> bool:
    """Whether Python code sets members from strings or expressions, so what it gives cannot be read off its names:
    setattr, type() with three arguments, exec or eval, globals(), vars() or locals(), a __dict__ or __setattr__, a
    class or namespace whose members are named in strings or keywords (namedtuple, make_dataclass, an Enum call,
    SimpleNamespace), or a store into a subscript (x[key] = ...)."""
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            called = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if called in _PY_DYNAMIC or called == "type" and len(n.args) == 3:
                return True
        elif isinstance(n, ast.Attribute) and n.attr in ("__dict__", "__setattr__") or \
                isinstance(n, ast.Subscript) and not isinstance(n.ctx, ast.Load):
            return True
    return False


def _js_dynamic(text) -> bool:
    """Whether JS/TS code sets members from strings or expressions: a class expression (globalThis.Neu = class {...},
    { Neu: class {...} }), Object.assign, Object.defineProperty and their kind, Reflect, eval or new Function, or a
    store into a computed member (x[key] = ...)."""
    return bool(_JS_DYNAMIC.search(_js_mask(text)))


def _undone_names(f, r) -> tuple:
    """(the names whose meaning the restore `r` (_rebuild_helpers) changes, the keys of the helpers it rebuilt whose
    meaning cannot be told). The names: each that a helper it rebuilt names now and not at the base, or the other way
    round; the name of each function it rebuilt whose parameters changed; and what the code the change adds gives (its
    names, a class's members, the attributes it sets) when a rebuilt helper names it only now, or reaches it through
    other added code: without the change it no longer makes or calls it. Added code: a helper the change adds, and a
    statement outside the tests, helpers and imports that it adds or edits (r["scope"]). The keys: the rebuilt
    helpers that reach added code whose members are set from strings or expressions (_py_dynamic, _js_dynamic; such
    a statement always): what that code gives cannot be named. A helper it put back after the change removed it
    changes none: it comes back as it was. A name an import binds in the rebuilt module means the same with and
    without the change, and is none."""
    py = _lang(f) == "py"
    ob, nb = {k: x for k, *x in r["old"]}, {k: x for k, *x in r["now"]}
    old, now = r["lines"]
    added = []  # (key, or None for a statement; the names it reads; what it gives; whether its members are dynamic)
    for k in [k for k in r["added"] if k in nb]:
        s, e, kind, node = nb[k]
        if py:
            added.append((k, _py_names(node, kind), _py_gives(node, kind), _py_dynamic(node)))
        else:
            text = "".join(now[s - 1:e])
            added.append((k, _js_names(text), _js_gives(text) | {k[0], k[1]} - {"", "<head>"}, _js_dynamic(text)))
    added += [(None, words, set(), True) for words in r["scope"]]
    out, dynamic = set(), set()
    for k in [k for k in r["undone"] if k in nb]:
        (a, b, kind_a, node_a), (s, e, kind, node) = ob[k], nb[k]
        if py:
            before, after = _py_names(node_a, kind_a), _py_names(node, kind)
            fn = (ast.FunctionDef, ast.AsyncFunctionDef)
            if isinstance(node_a, fn) and isinstance(node, fn) and ast.dump(node_a.args) != ast.dump(node.args):
                out.add(k[1])
        else:
            was, is_ = "".join(old[a - 1:b]), "".join(now[s - 1:e])
            before, after = _js_names(was), _js_names(is_)
            if _js_params(was) != _js_params(is_):
                out.add(k[1])
        out |= before ^ after
        reach, todo = after - before, list(added)
        while True:  # the added code those names reach, one step or more, and the members of an added class reached
            hit = [x for x in todo if (x[1] & reach if x[0] is None else
                                       x[0][1] in reach or x[0][0] and x[0][0] in reach)]
            if not hit:
                break
            for x in hit:
                todo.remove(x)
                reach |= x[1]
                out |= x[2]
                if x[3]:
                    dynamic.add(k)
    return out - (_py_imported if py else _js_imported)(r["text"] or "".join(now)), dynamic


def _reads_undone(f, r) -> set:
    """The tests of test module `f` that cannot be proven although its helpers came back as their base text: those
    whose own text, or a helper the change adds that they reach, names a word whose meaning the restore `r` changes
    (_undone_names), and those that reach a rebuilt helper whose meaning cannot be told. Without the change such a
    test fails on that name, a name only the change's helper edit gives, whatever the rest of the change does. A test
    reaches a helper it names, every member of a class it names, and every one the test framework calls for it
    (_framework_reach). -> their names, as _units gives them."""
    names, dynamic = _undone_names(f, r)
    if not names and not dynamic:
        return set()
    py, now = _lang(f) == "py", r["lines"][1]
    nb = {k: x for k, *x in r["now"]}

    def words(s, e, kind, node, outside=False):
        return _py_names(node, kind, outside) if py else _js_names("".join(now[s - 1:e]))

    def called(k):
        return _framework_reach(_lang(f), now, k[1], nb[k][0], nb[k][1],
                                k[0] if py and nb.get(("", k[0], 0), (0, 0, ""))[2] == "tests" else "")
    added = [(k, words(*nb[k]), called(k)) for k in r["added"]]
    unknown = [(k, called(k)) for k in dynamic]
    out = set()
    for (cls, name, _), s, e, kind, node in r["now"]:
        if kind != "fixed":
            continue
        seen = words(s, e, kind, node) | (_py_names(nb[("", cls, 0)][3], "head", False) if py and cls else set())
        todo = list(added)
        while True:
            hit = [a for a in todo if a[0][1] in seen or a[0][0] and a[0][0] in seen or a[2] == "every" or
                   a[2] == "class" and a[0][0] == cls]
            if not hit:
                break
            for a in hit:
                seen |= a[1]
                todo.remove(a)
        if seen & names or any(k[1] in seen or k[0] and k[0] in seen or reach == "every" or
                               reach == "class" and k[0] == cls for k, reach in unknown):
            out.add(f"{cls}.{name}" if cls else name)
    return out


# the test framework calls these members of a test class itself, these module-level helpers of a test module, and
# these JavaScript hooks
_CLASS_CALLED = {"run", "__init__", "__call__", "debug", "setUp", "tearDown", "setUpClass", "tearDownClass",
                 "asyncSetUp", "asyncTearDown", "setup_method", "teardown_method", "setup_class", "teardown_class"}
_MODULE_CALLED = {"setUpModule", "tearDownModule", "setup_module", "teardown_module", "setup_function",
                  "teardown_function", "pytestmark", "load_tests"}
_JS_HOOK_NAMES = {"beforeEach", "afterEach", "beforeAll", "afterAll"}


def _framework_reach(lang, src, name, s, e, cls) -> str:
    """The tests a helper reaches because the test framework calls it, whatever names it: "class" for a member of a
    test class the framework calls (run, __init__, __call__, setUp and its kind, a dunder), "every" for such a
    module-level helper (a module setup, pytestmark, load_tests, a pytest hook, a fixture, which pytest finds by its
    name= alias and a conftest fixture may ask for) and for a JavaScript hook, "" for any other. `src`: the module's
    lines, `s`..`e` the helper's, `cls` its test class or ''."""
    if lang == "js":
        return "every" if not name or name in _JS_HOOK_NAMES else ""
    if cls:
        return "class" if name in _CLASS_CALLED or name[:2] == name[-2:] == "__" else ""
    head = []  # its decorators: the lines before its def
    for line in src[s - 1:e]:
        if line.lstrip().startswith(("def ", "async def ", "class ")):
            break
        head.append(line)
    called = name in _MODULE_CALLED or name.startswith(("pytest_", "__")) or re.search(r"\bfixture\b", "\n".join(head))
    return "every" if called else ""


def _py_support(text) -> list:
    """The named test support of a Python test module: [(name, first line, last line, class or '')] of its
    module-level functions that are no tests, its assignments and classes without tests, and the methods and class
    attributes of its test classes that are no tests."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    fn = (ast.FunctionDef, ast.AsyncFunctionDef)

    def span(n):
        return min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])]), getattr(n, "end_lineno", n.lineno)

    def names(n):
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            return [t.id for t in targets if isinstance(t, ast.Name)]
        return [n.name]

    out, plain = [], set()
    for node in tree.body:
        if isinstance(node, fn) and not node.name.startswith("test") or isinstance(node, (ast.Assign, ast.AnnAssign)):
            out += [(x, *span(node), "") for x in names(node)]
        elif isinstance(node, ast.ClassDef):
            mine = [i for i in node.body if isinstance(i, fn)]
            if not _py_test_class(node, plain) or not any(i.name.startswith("test") for i in mine):
                out.append((node.name, *span(node), ""))
            else:
                out += [(x, *span(i), node.name) for i in node.body
                        if isinstance(i, fn) and not i.name.startswith("test") or isinstance(i, (ast.Assign, ast.AnnAssign))
                        for x in names(i)]
    return out


def _body(lines, s, e) -> tuple:
    """Lines s..e of a file as they read: trailing spaces, blank lines and comment-only lines left out."""
    return tuple(x.rstrip() for x in lines[s - 1:e] if x.strip() and not x.strip().startswith(("#", "//")))


def _keyed(units, lines) -> dict:
    """(name, occurrence) -> body, for units that share a name (two describes with an 'adds' each)."""
    out, seen = {}, {}
    for n, s, e in units:
        k = seen[n] = seen.get(n, -1) + 1
        out[(n, k)] = _body(lines, s, e)
    return out


def _changed_units(top, base, f):
    """Names of the tests in `f` that are new or changed since `base`; None when the whole file counts.

    A test is changed when its own text differs from the same-named test's at `base` (so a test moved or with new
    tests inserted around it stays unchanged, whatever lines git's diff puts it next to), or when it reaches a test
    helper of the file that changed: a helper it calls, directly or through other helpers; a helper the test framework
    calls (_framework_reach: setUp, run, a beforeEach, a module setup, a fixture) reaches every test it runs for. Only
    a helper the change adds that no test reaches changes no test ([]): one that was there may be reached in ways no
    name shows, so a change to it that no test names counts the whole file, as a change elsewhere (an import) does."""
    p = subprocess.run(["git", "-C", top, "cat-file", "blob", f"{base}:{f}"], capture_output=True, env=core.GIT_ENV)
    if p.returncode != 0:
        return None
    old_text = p.stdout.decode("utf-8", "replace")
    text = _read(os.path.join(top, f))
    src = text.splitlines()
    lines = {ln for ln in _changed_lines(top, base, f)  # blank and comment-only lines change no test
             if 0 < ln <= len(src) and src[ln - 1].strip() and not src[ln - 1].strip().startswith(("#", "//"))}
    units = _units(f, text)
    if not lines or not units:
        return None
    tests = [(n, s, e) for n, s, e in units if not n.endswith(".*")]
    old_lines = old_text.splitlines()
    before = _keyed([u for u in _units(f, old_text) if not u[0].endswith(".*")], old_lines)
    now = _keyed(tests, src)
    picked = {k[0] for k, body in now.items() if before.get(k) != body}
    outside = {ln for ln in lines if not any(s <= ln <= e for _, s, e in tests)}
    helpers_only = False  # every changed line outside the tests is inside a helper the change adds
    if outside:
        reached, helpers_only = _helper_callers(f, text, old_text, units, tests, outside)
        picked |= reached
    return sorted(picked) if picked or helpers_only else None


def _helper_callers(f, text, old_text, units, tests, lines) -> tuple:
    """The tests of `f` a change to its support reaches (see _changed_units); `lines`: the changed lines outside
    every test. -> (the tests, whether every line of `lines` is inside a helper the change adds)."""
    src, lang = text.splitlines(), _lang(f)
    if lang == "py":
        support = _py_support(text)
        old = {(c, n): _body(old_text.splitlines(), s, e) for n, s, e, c in _py_support(old_text)}
        members, had = _py_members(text), set(_py_members(old_text))
    elif lang == "js":
        support = [(n, s, e, "") for n, s, e in _js_support(text)]
        old = {(c, n): _body(old_text.splitlines(), s, e) for n, s, e, c in
               [(n, s, e, "") for n, s, e in _js_support(old_text)]}
        members, had = _js_members(text), set(_js_members(old_text))
    else:
        return {n for n, _, _ in tests}, False  # no helpers read here: every test, as a change outside the tests was
    every, picked, changed = {n for n, _, _ in tests}, set(), set()
    covered = set()
    for name, s, e, cls in support:
        mine = {ln for ln in lines if s <= ln <= e}
        if not mine:
            continue
        covered |= mine
        reach = _framework_reach(lang, src, name, s, e, cls)
        if reach == "every":
            picked |= every
        elif reach == "class":
            picked |= {n for n in every if n.startswith(cls + ".")}
        elif name and old.get((cls, name)) != _body(src, s, e):
            # a helper the change adds is changed too: a test that calls it and did not change called another of
            # that name before (one a base class gives, or one imported)
            changed.add(name)
    for n, s, e in units:  # a change inside a test class outside its tests and named members: all its tests
        if n.endswith(".*") and {ln for ln in lines - covered if s <= ln <= e}:
            picked |= {t for t in every if t.startswith(n[:-1])}
    reach, todo = set(changed), list(changed)
    while todo:  # helpers that call a changed helper are changed too
        rx = re.compile(r"(?<![\w$])" + re.escape(todo.pop()) + r"(?![\w$])")
        for name, s, e, _ in support:
            if name and name not in reach and rx.search("\n".join(src[s - 1:e])):
                reach.add(name)
                todo.append(name)
    if reach:
        rx = re.compile(r"(?<![\w$])(?:" + "|".join(map(re.escape, sorted(reach))) + r")(?![\w$])")
        picked |= {n for n, s, e in tests if rx.search("\n".join(src[s - 1:e]))}
    # the helpers the change adds, members of a class without tests too
    added = [(s, e) for name, s, e, cls in support if name and (cls, name) not in old] + \
        [span for k, span in members.items() if k not in had]
    return picked, all(any(s <= ln <= e for s, e in added) for ln in lines)


def _ut_module(runner, rel) -> str:
    """A test file's module name for unittest: from the start folder (test_x), or from the runner's root
    (tests.test_x) when the start folder is a package, which unittest then imports with its __init__.py."""
    start = runner.get("start") or "."
    sub = os.path.relpath(rel, start).replace("\\", "/") if start != "." and not runner.get("package") else rel
    return re.sub(r"\.py$", "", rel if sub.startswith("../") else sub).replace("/", ".")


def _ut_name(runner, id_: str) -> str:
    """A unittest id to run: in a package start folder, an id recorded by its bare module name (test_x.C.t, from a
    run before the folder became a package) gets the package's dotted prefix (tests.test_x.C.t)."""
    if not runner.get("package"):
        return id_
    pkg = os.path.normpath(runner.get("start") or ".").replace("\\", "/").replace("/", ".")
    return id_ if id_ == pkg or id_.startswith(pkg + ".") else f"{pkg}.{id_}"


def _unit_ids(top, runner, f, units):
    rel, n = _rel(top, runner, f), runner["name"]
    if n == "unittest":
        return [f"{_ut_module(runner, rel)}.{u}" for u in units]
    if n == "pytest":
        return [f"{rel}::{u.replace('.', '::')}" for u in units]
    if n == "maven":
        return [f"{_jvm_class(rel)}#{u}" for u in units]
    if n == "gradle":
        return [{"id": f"{_jvm_class(rel)}.{u}", "file": f} for u in units]
    if n == "go":
        return [f"{os.path.dirname(f) or '.'}:{u}" for u in units]
    if n in ("phpunit", "pest"):
        return [f"{_split(f)[2]}::{u}" for u in units]
    return None  # jest/vitest: the file runs, the changed titles are picked from the results


def _norm(i: str) -> str:
    return re.sub(r"(\[.*\]|\(.*\))$", "", i)


def _member(case, f, units, ids):
    """Does a result case belong to the changed tests of file `f`?"""
    if ids:
        cid = case["id"]
        return any(cid == i or _norm(cid) == i or cid.endswith("\\" + i) or cid.startswith(i + "[")
                   for i in (_id(x) for x in ids))
    same = case.get("test_file", case["file"]) == f or (
        f.endswith(".go") and case["file"].startswith((os.path.dirname(f) or ".") + "/"))
    return same and (units is None or any(_title_matches(case["name"], u) for u in units))


_TITLE_SLOT = re.compile(r"%[sdifjoOpc#]|\$[A-Za-z_$][\w$]*(?:\.[\w$]+)*")


def _title_matches(name: str, unit: str) -> bool:
    """Whether a result's name is the test `unit` names: it ends with the title, or, for a title template (it.each's
    %i, %s, $a), with the title its case filled in."""
    if name.endswith(unit):
        return True
    if not _TITLE_SLOT.search(unit):
        return False
    parts = _TITLE_SLOT.split(unit.replace("%%", "\0"))
    rx = ".+?".join(re.escape(x).replace("\0", "%") for x in parts)
    return re.search(r"(?:^|\W)" + rx + r"$", name) is not None


def _copy_files(top) -> list:
    """The files a proof copy holds: HEAD plus uncommitted changes (tracked and untracked, not ignored)."""
    return _ls(top, "-co", "--exclude-standard")


def _dep_folders(top, listed) -> list:
    """The checkout's dependency folders a proof copy links (at the folders of package manifests) -> [relative path]."""
    roots = {""} | {f.rpartition("/")[0] for f in listed if f.rpartition("/")[2] in (
        "package.json", "composer.json", "pyproject.toml", "go.mod", "setup.py", "requirements.txt")}
    return [os.path.join(d, dep) for d in sorted(roots) for dep in DEP_DIRS if os.path.isdir(os.path.join(top, d, dep))]


def _copy_worktree(top, dest, listed=None) -> list:
    """HEAD plus uncommitted changes (tracked and untracked, not ignored); dependency folders are linked.
    `listed`: the files to copy (default _copy_files). -> the links made in `dest` to the checkout's dependency
    folders."""
    linked = []
    listed = _copy_files(top) if listed is None else listed
    for f in dict.fromkeys(listed):
        src, dst = os.path.join(top, f), os.path.join(dest, f)
        if not os.path.lexists(src):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
        elif os.path.isfile(src):
            shutil.copy2(src, dst)
    for rel in _dep_folders(top, listed):
        real, link = os.path.join(top, rel), os.path.join(dest, rel)
        if not os.path.lexists(link):
            os.makedirs(os.path.dirname(link), exist_ok=True)
            os.symlink(real, link)
            linked.append(link)
    return linked


def _copy_source_paths(top, dest, linked) -> list:
    """The copy's own folders for the checkout folders that the .pth files of a linked virtual environment name.
    An editable install writes one: `<checkout>/src` for a src layout. The copy links the checkout's environment, so
    without these its Python imports the checkout's code, where the change is never undone. On PYTHONPATH they come
    before site-packages, where Python adds the .pth paths. A .pth line that runs code (`import …`) names no folder."""
    real_top = os.path.realpath(top)
    out = []
    for link in linked:
        if os.path.basename(link) not in (".venv", "venv"):
            continue
        out += [p for p in _copy_mapped_packages(real_top, dest, link) if p not in out]
        for pth in sorted(Path(link).glob("lib/python*/site-packages/*.pth")) + \
                sorted(Path(link).glob("Lib/site-packages/*.pth")):
            try:
                lines = pth.read_text(errors="replace").splitlines()
            except OSError:
                continue
            for ln in (x.strip() for x in lines):
                if not ln or ln.startswith("#") or re.match(r"import[ \t]", ln):
                    continue
                p = os.path.realpath(os.path.join(os.path.dirname(pth), ln))
                if p != real_top and not p.startswith(real_top + os.sep):
                    continue
                mine = os.path.join(dest, os.path.relpath(p, real_top))
                if os.path.isdir(mine) and mine not in out:
                    out.append(mine)
    return out


def _mapping(finder: Path) -> dict:
    """The MAPPING (top-level name -> folder, or a module's path without .py) of a setuptools editable finder, read as
    a literal; the finder's code never runs. {} when it holds none."""
    try:
        tree = ast.parse(finder.read_text(errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return {}
    for node in tree.body:
        target = node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else \
            node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(target, ast.Name) and target.id == "MAPPING" and node.value is not None:
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return {}
            return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)} \
                if isinstance(value, dict) else {}
    return {}


def _copy_mapped_packages(real_top, dest, venv) -> list:
    """setuptools installs a package whose folder has another name (package-dir) through an import hook, not a .pth
    path: `__editable___<name>_finder.py`, whose MAPPING sends `pkg` to the checkout's `src/pkg_impl`, so the copy's
    Python imports the checkout's code. For each mapped name inside the checkout, a folder beside the copy
    (<copy>-paths, removed with it) gets a link of that name to the copy's own folder or module -> [that folder] to
    put first on PYTHONPATH, where Python finds the name before the hook runs; [] when nothing is mapped."""
    links = Path(str(dest) + "-paths")
    made = False
    for finder in sorted(Path(venv).glob("lib/python*/site-packages/__editable___*_finder.py")) + \
            sorted(Path(venv).glob("Lib/site-packages/__editable___*_finder.py")):
        for name, target in _mapping(finder).items():
            p = os.path.realpath(target)
            if "." in name or not p.startswith(real_top + os.sep):
                continue
            mine = os.path.join(str(dest), os.path.relpath(p, real_top))
            src, at = (mine, links / name) if os.path.isdir(mine) else \
                (mine + ".py", links / f"{name}.py") if os.path.isfile(mine + ".py") else \
                (mine, links / os.path.basename(mine)) if mine.endswith(".py") and os.path.isfile(mine) else (None, None)
            if src is None or os.path.lexists(at):
                continue
            links.mkdir(exist_ok=True)
            os.symlink(src, at)
            made = True
    return [str(links)] if made else []


def _built_installs(top, dest, linked, listed) -> tuple:
    """The checkout's own packages that a linked virtual environment holds as a built copy (not editable: `uv sync
    --no-editable`, `pip install .`): site-packages holds the files built from the changed source, and no .pth leads
    Python back to a source folder, so the copy's Python imports that build and the restored code is never run. A
    dist-info whose direct_url.json names a folder of the checkout is one; its RECORD lists the files it installed.
    For each top-level name, the copy's folder that holds the same file (`src` for `src/pkg/mod.py`) goes first on
    PYTHONPATH. -> ([those folders], [(package, environment) no folder of the copy holds])."""
    real_top = os.path.realpath(top)
    have = set(listed)
    out, stuck = [], []
    for link in linked:
        if os.path.basename(link) not in (".venv", "venv"):
            continue
        for info in sorted(Path(link).glob("lib/python*/site-packages/*.dist-info")) + \
                sorted(Path(link).glob("Lib/site-packages/*.dist-info")):
            origin = core.read_json(info / "direct_url.json", {}) or {}
            url = str(origin.get("url") or "") if isinstance(origin, dict) else ""
            if not url.startswith("file://") or (origin.get("dir_info") or {}).get("editable"):
                continue
            src = os.path.realpath(urllib.parse.unquote(urllib.parse.urlparse(url).path))
            if os.path.isfile(src):  # a wheel built into the checkout (dist/…whl): its sources are the project's
                src = os.path.dirname(os.path.dirname(src))
            if src != real_top and not src.startswith(real_top + os.sep):
                continue
            project = os.path.relpath(src, real_top).replace(os.sep, "/")
            project = "" if project == "." else project + "/"
            entries = {}  # top-level name -> its shortest installed module path
            for line in _read(info / "RECORD").splitlines():
                f = line.rsplit(",", 2)[0] if line.count(",") >= 2 else line
                head = f.split("/", 1)[0]
                if not f.endswith(".py") or f.startswith("../") or head.endswith((".dist-info", ".data")) or \
                        "__pycache__" in f.split("/"):
                    continue
                name = head[:-3] if head.endswith(".py") else head
                if name not in entries or len(f) < len(entries[name]):
                    entries[name] = f
            env = os.path.relpath(link, str(dest)).replace(os.sep, "/")
            for name, f in sorted(entries.items()):
                # the copy's file at <project><prefix><f>, the shallowest prefix
                prefixes = sorted((h[len(project):-len(f)] for h in have
                                   if h.startswith(project) and (h[len(project):] == f or
                                                                 h[len(project):].endswith("/" + f))),
                                  key=lambda p: (p.count("/"), p))
                if not prefixes:
                    stuck.append({"package": name, "environment": env})
                    continue
                mine = os.path.normpath(os.path.join(str(dest), project + prefixes[0]))
                if mine not in out:
                    out.append(mine)
    return out, stuck


def _restore_base(top, base, dest, files) -> None:
    for f in files:
        dst = os.path.join(dest, f)
        p = subprocess.run(["git", "-C", top, "cat-file", "blob", f"{base}:{f}"], capture_output=True,
                           env=core.GIT_ENV)
        if p.returncode == 0:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.islink(dst):
                os.remove(dst)
            with open(dst, "wb") as fh:
                fh.write(p.stdout)
        elif os.path.lexists(dst):
            os.remove(dst)
    for d, subs, _ in os.walk(dest):
        subs[:] = [s for s in subs if s not in DEP_DIRS and s != ".git"]
        if "__pycache__" in subs:
            shutil.rmtree(os.path.join(d, "__pycache__"), ignore_errors=True)
            subs.remove("__pycache__")


def _carry_git(top, dest) -> str:
    """Give the copy at `dest` the checkout's git repository, so a test that runs git on its own checkout (its files,
    branch, history or origin) sees in the copy what it sees in the checkout: a clone that shares the checkout's
    objects (no history is copied), with its branches, remote branches and tags, HEAD on the same branch and commit,
    the index at that commit and the checkout's origin URL. The work files stay the copy's. Returns '' when the copy
    has the repository, else why not (the copy then has none)."""
    parent = tempfile.mkdtemp(prefix=f"{PROVE_COPY}git-{os.getpid()}-", dir=os.path.dirname(str(dest)))
    try:
        clone = os.path.join(parent, "c")
        rc, _, err = core.sh(["git", "clone", "-q", "--shared", "--no-checkout", str(top), clone], timeout=120)
        if rc != 0:
            return f"git clone: {err or rc}"
        os.rename(os.path.join(clone, ".git"), os.path.join(str(dest), ".git"))
    finally:
        shutil.rmtree(parent, ignore_errors=True)
    head = _git(top, "rev-parse", "--verify", "-q", "HEAD")
    branch = _git(top, "symbolic-ref", "-q", "HEAD")
    url = _git(top, "config", "--get", "remote.origin.url")
    steps = [["fetch", "-q", "--prune", "--update-head-ok", str(top), "+refs/heads/*:refs/heads/*",
              "+refs/remotes/*:refs/remotes/*", "+refs/tags/*:refs/tags/*"],
             ["symbolic-ref", "HEAD", branch] if branch else ["update-ref", "--no-deref", "HEAD", head],
             ["reset", "-q"],
             ["remote", "set-url", "origin", url] if url else ["remote", "remove", "origin"]]
    for args in steps:
        rc, _, err = core.git(dest, *args, timeout=120)
        if rc != 0:
            shutil.rmtree(os.path.join(str(dest), ".git"), ignore_errors=True)
            return f"git {args[0]}: {err or rc}"
    return ""


_NODE_NAMED = re.compile(r"^test-|[-_]test$|^test$")


def _node_script(repo, runners, f, named=False) -> bool:
    """Does the node runner take `f` as a test although its name has no .test or .spec? node --test runs test-*,
    *-test, *_test and test.* scripts and every script in a test folder. A script there that another file of its
    folder imports or names in quotes is a helper (a harness, a fake binary), unless it is `named` on the command
    line."""
    r = owner(runners, f)
    p, name, stem, ext = _split(f)
    if r is None or r["name"] != "node" or ext not in (".js", ".mjs", ".cjs"):
        return False
    if _NODE_NAMED.search(stem):
        return True
    dirs = set(p.split("/")[:-1])
    if not dirs & {"test", "tests"} or dirs & (_SUPPORT_DIRS | {"fixtures", "__fixtures__", "testdata"}):
        return False
    if named:
        return True
    folder = p.rpartition("/")[0]
    named_by = re.compile(r"[\"'`/]" + re.escape(name) + r"[\"'`]")
    return not any(named_by.search(repo.text(g)) for g in repo.existing
                   if g != p and g.rpartition("/")[0] == folder and _lang(g) == "js")


def _no_runner(a, f) -> str:
    """Why no runner here takes test file `f`, and what would."""
    if getattr(a, "runner", None):
        return f"--runner {a.runner} leaves it out"
    if _lang(f) == "js":
        return "no runner here takes it: node is not on the PATH" if not shutil.which("node") else \
            "no runner here takes it: a package.json test script that runs jest, vitest or node --test makes one, " \
            "unless it builds first (a pretest script, tsc && node --test) or passes a node option devkit does not " \
            "know without `=`"
    return "no runner here takes it (dev test plan lists the runners found)"


_IMPORTS_ADDED = "cannot be proven: imports a file the change adds"
_IMPORT_STOPPED = "cannot be proven: stops at an import without the change"
_SETUP_STOPPED = "cannot be proven: stops in its class or module setup without the change"
# inconclusive outcomes, listed, never red
_CHECKOUT_IMPORT = "cannot be proven: imported the checkout's "  # + the changed files it imported
_NO_IMPORT_RECORD = "cannot be proven: no record of what the run imported"
_NOT_FROM_COPY = "cannot be proven: imported none of the changed files from the copy"
_HELPERS_TANGLED = "cannot be proven: its module's helper change cannot be undone apart from the test code it adds"

_NOT_PROVEN = ("skipped", "expected failure", _IMPORTS_ADDED, _IMPORT_STOPPED, _SETUP_STOPPED, _NO_IMPORT_RECORD,
               _NOT_FROM_COPY, _HELPERS_TANGLED)


def _not_proven(verdict: str) -> bool:
    return verdict in _NOT_PROVEN or verdict.startswith(_CHECKOUT_IMPORT)


# The run without the change records which files each Python process ran: this sitecustomize, first on the copy's
# PYTHONPATH, adds an audit hook before it runs the project's own sitecustomize (the next one on the path). The hook
# sees each code object a module runs (the "exec" event: an import, runpy, a cached .pyc under its source's name), so
# an import inside patch.dict(sys.modules), or one undone before the process ends, is still recorded; and it sees each
# process the run starts: a Python child that skips the recorder (-I, -E or -S, or an environment whose PYTHONPATH
# leaves out the recorder's folder) is written down as unrecorded, since what it imported is unknown; and it sees each
# file opened for reading (the "open" event), so a changed data file the run read from the copy counts as evidence too.
# Each process appends to its own record (<pid>.jsonl) as it goes: a path per line, {"read": <path>}, or
# {"unrecorded": <command>}. The records
# folder and the roots are written into this file, so a suite that clears its environment loses neither.
_IMPORT_RECORDER = """import _thread, json, os, shlex, sys

_OUT = %(out)r
_ROOTS = [r.lower() for r in %(roots)r]
_HERE = %(here)r
_seen, _read, _opened, _busy = set(), set(), set(), set()  # _read: the files read, resolved; _opened: their
# spellings as opened; _busy: the threads inside the hook, so its own work raises no event
_PY = ("python", "python3", "pythonw")
_SHELLS = ("sh", "bash", "zsh", "dash", "ksh")
_SEPARATORS = (";", "&", "|")
_PRINTERS = ("echo", "printf")  # programs that print their words and start none of them


def _write(item):
    with open(os.path.join(_OUT, "%%d.jsonl" %% os.getpid()), "a") as fh:
        fh.write(json.dumps(item) + "\\n" if item is not None else "")


def _python(word):
    base = os.path.basename(word).lower().removesuffix(".exe")
    return base.rstrip("0123456789.") in _PY or os.path.realpath(word) == os.path.realpath(sys.executable)


def _skips(argv, env):
    \"\"\"Whether the Python command argv (with env, None: this process's) runs without the recorder.\"\"\"
    i = 1
    while i < len(argv):
        w = argv[i]
        if w == "--check-hash-based-pycs":  # the one long option that takes a value, the next word
            i += 2
            continue
        if not w.startswith("-") or w in ("-", "--") or w.startswith(("-c", "-m", "--")):
            break
        if w.startswith(("-W", "-X")):
            i += 2 if w in ("-W", "-X") else 1  # a separate value is the next word
            continue
        if set(w[1:]) & set("IES"):
            return True
        i += 1
    path = (os.environ if env is None else env).get("PYTHONPATH", "")
    return _HERE not in [os.path.realpath(p) for p in os.fsdecode(path).split(os.pathsep) if p]


def _assignment(w):
    name, eq, _ = w.partition("=")
    return bool(eq) and name.isidentifier()


def _unwrap(argv, env):
    \"\"\"argv without its leading NAME=value words and env wrappers (/usr/bin/env -i A=1 …) -> (argv, the environment
    the rest runs with).\"\"\"
    argv, env = list(argv), dict(os.environ if env is None else env)
    while argv:
        if _assignment(argv[0]):
            k, _, v = argv.pop(0).partition("=")
            env[k] = v
        elif os.path.basename(argv[0]) == "env":
            argv.pop(0)
            while argv and (argv[0].startswith("-") or _assignment(argv[0])):
                w = argv.pop(0)
                if w == "--":
                    break
                if w in ("-", "-i", "--ignore-environment"):
                    env = {}
                elif _assignment(w):
                    k, _, v = w.partition("=")
                    env[k] = v
                elif w.startswith("--unset="):
                    env.pop(w[8:], None)
                elif w in ("-u", "--unset", "-C", "--chdir", "-P") and argv:
                    v = argv.pop(0)
                    if w in ("-u", "--unset"):
                        env.pop(v, None)
                elif w in ("-S", "--split-string") and argv:
                    argv = shlex.split(argv.pop(0)) + argv
        else:
            break
    return argv, env


def _shell_line(argv):
    \"\"\"The command line a shell's argv runs (sh -c, bash -lc, sh -ec), or None.\"\"\"
    i, command = 1, False
    while i < len(argv) and argv[i][:1] in "-+" and argv[i] not in ("-", "--"):
        w = argv[i]
        if w in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):  # an option whose value is the next word
            i += 2
            continue
        command = command or (not w.startswith("--") and "c" in w[1:])
        i += 1
    if command and i < len(argv) and argv[i] == "--":  # bash -c -- 'line': the line follows the end of options
        i += 1
    return argv[i] if command and i < len(argv) else None


def _pythons(argv, env, depth=0):
    \"\"\"The Python commands argv starts -> [(argv, environment)].\"\"\"
    argv, env = _unwrap(argv, env)
    if not argv or depth > 4:
        return []
    if _python(argv[0]):
        return [(argv, env)]
    if os.path.basename(argv[0]) in _SHELLS:
        line = _shell_line(argv)
        return _in_line(line, env, depth + 1) if line is not None else []
    if os.path.basename(argv[0]) in _PRINTERS:
        return []
    # a program that runs the rest of its words (timeout 30 python, nice -n 5 python, xargs sh -c …, timeout 30 env -i
    # python): what starts at its first word that is a Python, a shell or env
    for i, w in enumerate(argv[1:], 1):
        if _python(w) or os.path.basename(w) in _SHELLS + ("env",):
            return _pythons(argv[i:], env, depth + 1)
    return []


def _substitutions(line):
    \"\"\"The command lines inside a shell line's $( ) and backticks, outside single quotes: each runs, whatever the
    command around it (echo, printf) does with its output.\"\"\"
    found, i, quote = [], 0, ""
    while i < len(line):
        c = line[i]
        if c == "\\\\" and quote != "'":
            i += 2
            continue
        if c in "'\\"" and quote in ("", c):  # a quote opens, or closes the one open
            quote = "" if quote == c else c
        elif quote != "'" and line.startswith("$(", i):
            depth, j = 0, i + 1
            while j < len(line):
                depth += {"(": 1, ")": -1}.get(line[j], 0)
                if not depth:
                    break
                j += 1
            found.append(line[i + 2:j])
            i = j
        elif quote != "'" and c == "`":
            j = line.find("`", i + 1)
            j = len(line) if j < 0 else j
            found.append(line[i + 1:j])
            i = j
        i += 1
    return found


def _in_line(line, env, depth=0):
    \"\"\"The Python commands a shell line starts: each command of it and of its command substitutions, or the first
    Python word in it (time python, nohup python …).\"\"\"
    if depth > 4:
        return []
    found = [x for inner in _substitutions(line) for x in _in_line(inner, env, depth + 1)]
    spaced = line
    for sep in _SEPARATORS:
        spaced = spaced.replace(sep, " %%s " %% sep)
    try:
        words = shlex.split(spaced)
    except ValueError:
        words = spaced.split()
    commands, here = [], []
    for w in words + [";"]:
        if w in _SEPARATORS:
            commands, here = commands + [here], []
        else:
            here.append(w)
    for c in commands:
        found += _pythons(c, env, depth)
    return found


def _words(x):
    if isinstance(x, (str, bytes, os.PathLike)):
        return [os.fsdecode(x)]
    return [os.fsdecode(w) for w in x or () if isinstance(w, (str, bytes, os.PathLike))]


def _env(e):
    if e is None or not hasattr(e, "items"):
        return None
    return {os.fsdecode(k): os.fsdecode(v) for k, v in e.items() if isinstance(v, (str, bytes))}


def _started(argv, found):
    if any(_skips(a, e) for a, e in found):
        _write({"unrecorded": " ".join(argv)[:300]})


def _hook(event, args):
    me = _thread.get_ident()
    if me in _busy:
        return
    _busy.add(me)
    try:
        if event == "exec":
            f = getattr(args[0], "co_filename", None)
            if isinstance(f, str) and f not in _seen and os.path.isabs(f):
                _seen.add(f)
                real = os.path.realpath(f)
                low = real.lower()
                if any(low == r or low.startswith(r + os.sep) for r in _ROOTS):
                    _write(real)
        elif event == "open" and isinstance(args[0], (str, bytes)) and args[0] not in _opened:
            # a file read for its data (a module's source is the exec event's): {"read": path}. An open that empties
            # the file first (w+, x+, O_TRUNC, O_EXCL) reads none of its old content.
            mode, flags = args[1], args[2] or 0
            reading = ("r" in mode or "a" in mode and "+" in mode) if isinstance(mode, str) else \\
                flags & getattr(os, "O_ACCMODE", 3) != os.O_WRONLY and not flags & (os.O_TRUNC | os.O_EXCL)
            if not reading:
                return
            _opened.add(args[0])  # this spelling was read: the next read of it is the same file
            real = os.path.realpath(os.fsdecode(args[0]))
            low = real.lower()
            if real not in _read and not low.endswith((".py", ".pyc")) and \\
                    any(low.startswith(r + os.sep) for r in _ROOTS):
                _read.add(real)
                _write({"read": real})
        elif event == "subprocess.Popen":
            exe, argv, env = args[0], _words(args[1]), _env(args[3])
            if isinstance(args[1], (str, bytes)):
                _started(argv, _in_line(argv[0], env))
            else:
                exe = _words(exe)[0] if exe is not None and _words(exe) else None
                # executable= runs that program with argv[0] as its name only
                run = [exe] + argv[1:] if exe and argv and _python(exe) and not _python(argv[0]) else argv
                _started(argv, _pythons(run, env))
        elif event in ("os.exec", "os.posix_spawn"):
            argv = _words(args[1])
            _started(argv, _pythons(argv, _env(args[2])))
        elif event == "os.spawn":
            argv = _words(args[2])
            _started(argv, _pythons(argv, _env(args[3])))
        elif event == "os.system":
            argv = _words(args[0])
            _started(argv, _in_line(argv[0], None) if argv else [])
    except Exception:
        pass
    finally:
        _busy.discard(me)


if _OUT:
    _write(None)
    sys.addaudithook(_hook)


def _project_sitecustomize():
    import importlib.machinery, importlib.util
    for p in list(sys.path):
        if os.path.realpath(p or os.curdir) == _HERE:
            continue
        spec = importlib.machinery.PathFinder.find_spec("sitecustomize", [p or os.curdir])
        if spec and spec.loader and os.path.dirname(os.path.realpath(spec.origin or "")) != _HERE:
            spec.loader.exec_module(importlib.util.module_from_spec(spec))
            break


_project_sitecustomize()
"""


def _import_recorder(dest, top, name="imports") -> tuple:
    """A folder with the recorder (_IMPORT_RECORDER) beside the copy, <dest>-<name> -> (folder for PYTHONPATH, records
    folder, environment for the run)."""
    folder = Path(f"{dest}-{name}")
    records = folder / "records"
    records.mkdir(parents=True, exist_ok=True)
    roots = [os.path.realpath(top), os.path.realpath(dest)]
    (folder / "sitecustomize.py").write_text(_IMPORT_RECORDER % {"out": str(records), "roots": roots,
                                                                 "here": os.path.realpath(folder)})
    return str(folder), str(records), {}


def _checkout_builds(linked, real_top) -> set:
    """The top-level names a linked virtual environment holds as a built copy of the checkout's own package (a
    dist-info whose direct_url.json names a folder of the checkout, not editable)."""
    names = set()
    for link in linked:
        if os.path.basename(link) not in (".venv", "venv"):
            continue
        for info in sorted(Path(link).glob("lib/python*/site-packages/*.dist-info")) + \
                sorted(Path(link).glob("Lib/site-packages/*.dist-info")):
            origin = core.read_json(info / "direct_url.json", {}) or {}
            url = str(origin.get("url") or "") if isinstance(origin, dict) else ""
            if not url.startswith("file://") or (origin.get("dir_info") or {}).get("editable"):
                continue
            src = os.path.realpath(urllib.parse.unquote(urllib.parse.urlparse(url).path))
            if src == real_top or src.startswith(real_top + os.sep) or real_top.startswith(src + os.sep):
                for line in _read(info / "RECORD").splitlines():
                    head = line.split(",", 1)[0].split("/", 1)[0]
                    if head.endswith(".py") or "/" in line.split(",", 1)[0]:
                        names.add(head[:-3] if head.endswith(".py") else head)
    return names


def _linked_checkout_sources(top, linked, changed) -> list:
    """The changed JS/TS files a linked node_modules reaches in the checkout: the copy links the checkout's
    node_modules, and its links to workspace packages (npm, pnpm and yarn workspaces) resolve there, into the
    checkout's own packages, where the change stays. A run that imports such a package loads the checkout's code."""
    real_top = os.path.realpath(top)
    js = [f for f in changed if _lang(f) == "js"]
    targets = set()
    for link in linked if js else ():
        if os.path.basename(link) != "node_modules":
            continue
        folder = os.path.realpath(link)
        entries = []
        with contextlib.suppress(OSError):
            for entry in os.scandir(folder):
                if entry.name.startswith("@") and entry.is_dir(follow_symlinks=False):
                    with contextlib.suppress(OSError):
                        entries += list(os.scandir(entry.path))
                else:
                    entries.append(entry)
        for entry in entries:
            if entry.is_symlink():
                t = os.path.realpath(entry.path)
                if t.startswith(real_top + os.sep) and "node_modules" not in t[len(real_top):].split(os.sep):
                    targets.add(t)
    return sorted(f for f in js if any(os.path.realpath(os.path.join(top, f)).startswith(t + os.sep)
                                       for t in targets))


def _file_ids(paths) -> dict:
    """The files of `paths` by identity (device, inode) -> {identity: path}: a path in another letter case on a
    case-insensitive disk, or through a link, is the same file."""
    out = {}
    for x in paths:
        with contextlib.suppress(OSError):
            st = os.stat(x)
            out[(st.st_dev, st.st_ino)] = x
    return out


def _import_evidence(records, top, dest, changed, linked) -> dict:
    """Where the run without the change imported the changed Python files from -> {from_copy, from_checkout,
    recorded, unrecorded}: from_copy, the files imported from the copy (the change undone), and the changed data files
    it read there; from_checkout, those
    imported from the checkout, where the change stays, or from a built copy of the checkout's package in its
    environment; recorded, how many processes left a record; unrecorded, the Python processes the run started that
    skipped the recorder. Files are compared by identity, not by how their path is spelled."""
    seen, read, recorded, unrecorded = set(), set(), 0, []
    for rec in sorted(Path(records).glob("*.jsonl")):
        recorded += 1
        for line in _read(rec).splitlines():
            with contextlib.suppress(ValueError):
                item = json.loads(line)
                if isinstance(item, str):
                    seen.add(item)
                elif isinstance(item, dict) and isinstance(item.get("unrecorded"), str):
                    unrecorded.append(item["unrecorded"])
                elif isinstance(item, dict) and isinstance(item.get("read"), str):
                    read.add(item["read"])
    real_top, real_dest = os.path.realpath(top), os.path.realpath(dest)
    builds = _checkout_builds(linked, real_top)
    ids = _file_ids(seen)

    def ran(path):
        with contextlib.suppress(OSError):
            st = os.stat(path)
            return (st.st_dev, st.st_ino) in ids
        return False

    from_copy, from_checkout = [], []
    for f in (x for x in changed if x.endswith(".py")):
        if ran(os.path.join(real_top, f)):
            from_checkout.append(f)
            continue
        inside = [x.split("/site-packages/", 1)[1] for x in seen if "/site-packages/" in x]
        if any(i.split("/", 1)[0].removesuffix(".py") in builds and (f == i or f.endswith("/" + i)) for i in inside):
            from_checkout.append(f)
        elif ran(os.path.join(real_dest, f)):
            from_copy.append(f)
    # a changed data file the run read from the copy is evidence too: the run saw it without the change
    data = _file_ids(read)
    for f in (x for x in changed if not x.endswith(".py")):
        with contextlib.suppress(OSError):
            st = os.stat(os.path.join(real_dest, f))
            if (st.st_dev, st.st_ino) in data:
                from_copy.append(f)
    linked_js = _linked_checkout_sources(top, linked, changed)
    return {"from_copy": sorted(from_copy), "from_checkout": sorted(from_checkout + linked_js), "recorded": recorded,
            "unrecorded": sorted(set(unrecorded)), "py": sorted(from_checkout), "js": linked_js}
# how a runner says a module or file could not be imported or found
_IMPORT_FAILED = re.compile(r"ModuleNotFoundError|ImportError|No module named|Failed to import|collection failure|"
                            r"Cannot find module|ERR_MODULE_NOT_FOUND|Failed to (?:load|resolve)|could not be resolved")
# a failed assertion that only mentions an import error (ImportError not raised) reached its assertion
_ASSERTION_FAILED = re.compile(r"^(?:E\s+)?(?:[\w.]+\.)?(?:AssertionError\b|assert\b|Expected\b|expect\()")
# a message that opens with an import or collection error: after pytest's `E`, Node's `Error:` or `Error [CODE]:`, or
# an error's module; vite's own words for a module it cannot load or resolve
_IMPORT_STOP = re.compile(r"(?:E\s+)?(?:Error(?:\s*\[\w+\])?:\s*)?(?:[\w.]+\.)?(?:ModuleNotFoundError|ImportError|"
                          r"No module named|Failed to import|collection failure|Cannot find module|"
                          r"ERR_MODULE_NOT_FOUND|Failed to load url|Failed to resolve import)")


def _stopped_at_import(message: str) -> bool:
    """The test stopped at an import or collection error, before any assertion ran: its message opens with one. A
    failure whose message only mentions one ("RuntimeError: Failed to load the settings") reached the code it tests."""
    msg = ui.ANSI_RE.sub("", message or "").strip()
    return bool(msg) and not _ASSERTION_FAILED.match(msg) and bool(_IMPORT_STOP.match(msg))


def _cover(res, f, id_):
    """For test `id_` of file `f`, which has no result of its own in `res`: the failure that stopped it before it ran
    -> ("load", message) when its file or module could not be loaded (a loader or collection failure; a run that
    died before any test reported, as unittest does when a named module raises anything but ImportError, or on an
    import error), ("setup", message) when the class or module setup around it failed, or None. Another test's
    failure, even in the same file, is no evidence about this one."""
    for c in res["cases"]:
        cid = c["id"]
        if c["status"] == "failed" and (cid == f or any(id_.startswith(cid + sep) for sep in (".", "::", "#"))):
            return ("load" if cid == f or c.get("load_failed") else "setup"), c["message"]
    if res["crash"]:
        died = [c["message"] for c in res["failures"]]
        died = [m for m in died if _IMPORT_FAILED.search(m or "")] or (died if not res["cases"] else [])
        if died:
            return "load", died[0]
    return None


def _proof_path(top):
    key = hashlib.sha256(str(Path(top).resolve()).encode()).hexdigest()
    return core.STATE / "lesson-proofs" / f"{key}.json"


PROOF_FILES = 4096
PROOF_BYTES = 64 * 1024 * 1024
_PROOF_CACHES = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def _proof_snapshot(top, files=None):
    """A bounded identity of the files a proof copy holds, or unknown ({}). Never follow external dependencies.

    `files`: the copy's list (default _copy_files(top)), so the checkout and its copy are compared over the same files:
    tracked and untracked ones, not ignored ones (build output, local caches), which the copy leaves out. Interpreter
    and tool caches are no input. Linked dependency trees and symbolic links cannot be certified by a checkout-only
    receipt. The bound is work (PROOF_FILES files, PROOF_BYTES bytes), never wall-clock time, so a loaded machine
    gets the same evidence. Hashes stay local.
    """
    root, hashes = Path(top).resolve(), {}
    remaining = PROOF_BYTES
    listed = _copy_files(str(root)) if files is None else list(files)
    if not listed or len(listed) > PROOF_FILES or _dep_folders(str(root), listed):
        return {}
    try:
        for rel in dict.fromkeys(listed):
            parts = rel.split("/")
            if _PROOF_CACHES & set(parts[:-1]) or parts[-1] == ".DS_Store":
                continue
            path = root / rel
            if path.is_symlink():
                return {}
            if not path.exists():
                continue  # deleted in the checkout: the copy leaves it out too
            if not path.is_file() or path.stat().st_size > remaining:
                return {}
            raw = path.read_bytes()
            remaining -= len(raw)
            if remaining < 0:
                return {}
            hashes[rel] = hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError):
        return {}
    return hashes


def _proof_runtime():
    """Identity of the isolated interpreter and fixed launcher; inherited Python settings are not inputs."""
    try:
        executable = str(Path(sys.executable).resolve())
        raw = Path(executable).read_bytes()
        return hashlib.sha256(raw + Path(__file__).read_bytes() +
                              json.dumps([executable, sys.version, "isolated-unittest-v1",
                                          _ISOLATED_UNITTEST]).encode()).hexdigest()
    except OSError:
        return ""


def prevention_proof(top, check: str, scope: str = "", *, cache=None):
    """Return fresh locally generated causal evidence for an exact check; never run a stored reference."""
    from . import state
    memo = {} if cache is None else cache
    key = str(Path(top).resolve())
    if ("receipt", key) not in memo:
        memo["receipt", key] = state.read(_proof_path(top)) or {}
    doc = memo["receipt", key]
    checks = doc.get("checks")
    if not isinstance(checks, dict):
        return None
    item = checks.get(check)
    if not isinstance(item, dict) or (scope and doc.get("scope") != scope):
        return None
    ts = item.get("ts")
    if type(ts) not in (int, float) or not 0 <= time.time() - ts <= 30 * 86400:
        return None
    if "runtime" not in memo:
        memo["runtime"] = _proof_runtime()
    if doc.get("snapshot_version") != 3 or doc.get("isolation") != "isolated-unittest-v1" or \
            doc.get("runtime") != memo["runtime"]:
        return None
    hashes = doc.get("hashes")
    if ("snapshot", key) not in memo:
        memo["snapshot", key] = _proof_snapshot(top)
    if not isinstance(hashes, dict) or not hashes or any(not isinstance(k, str) for k in hashes) or \
            memo["snapshot", key] != hashes:
        return None
    if item.get("without_change") != "failed" or item.get("verdict") != "proves the change":
        return None
    return item


def _record_prevention_proofs(top, rows, hashes, baseline, *, imports_attested=False):
    from . import state
    if not imports_attested or not hashes or _proof_snapshot(top) != hashes:
        return
    runtime = _proof_runtime()
    if not runtime:
        return
    proven = {f"{r['file']}::{r['id']}": dict(r, baseline=baseline, ts=int(time.time()))
              for r in rows if r["verdict"] == "proves the change" and r["without_change"] == "failed"}
    if not proven:
        return
    scope = project_key(top)[0]

    def save(doc):
        old = doc.get("checks") if (doc.get("scope") == scope and doc.get("hashes") == hashes
                                    and doc.get("runtime") == runtime) else {}
        doc.update(scope=scope, hashes=hashes, runtime=runtime, snapshot_version=3,
                   isolation="isolated-unittest-v1",
                   checks=dict(list({**(old or {}), **proven}.items())[-256:]))
    state.update(_proof_path(top), save)
    return True


PROVE_COPY = "devkit-prove-"
_PROVE_OWNER = re.compile(r"devkit-prove-(?:git-)?(\d+)-")
PROVE_COPY_DAYS = 1


def _process_gone(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OverflowError):
        return False
    return pid <= 1


def sweep_prove_copies(parent, now=None) -> list:
    """Remove the copies of proof runs that ended without removing their own (a crash, a closed terminal): folders
    named devkit-prove-* in `parent`, older than PROVE_COPY_DAYS, whose run's process is gone. A copy names that
    process (devkit-prove-<pid>-…); an older copy names none, and then only its age counts. -> the removed names."""
    return _sweep(parent, PROVE_COPY, _PROVE_OWNER, now)


def sweep_run_folders(parent, now=None) -> list:
    """Remove the folders of test runs killed before they removed their own (devkit-run-<pid>-…), as
    sweep_prove_copies does for proof copies: older than PROVE_COPY_DAYS, their process gone. -> the removed names."""
    return _sweep(parent, RUN_WORK, _RUN_OWNER, now)


def _sweep(parent, prefix, owner_name, now=None) -> list:
    now, gone = now or time.time(), []
    try:
        entries = list(os.scandir(parent))
    except OSError:
        return []
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not entry.is_dir(follow_symlinks=False) or st.st_uid != os.getuid() or \
                now - st.st_mtime < PROVE_COPY_DAYS * 86400:
            continue
        owner = owner_name.match(entry.name)
        if owner and not _process_gone(int(owner.group(1))):
            continue
        shutil.rmtree(entry.path, ignore_errors=True)
        if not os.path.lexists(entry.path):
            gone.append(entry.name)
    return gone


def _prove(a, top, runners) -> int:
    base, label = base_ref(top, a.since)
    if not base:
        return _refuse("nothing to compare with: no origin/<default> and no --since", "dev test prove --since <ref>")
    changed = changed_files(top, a.since)
    repo = _Repo(top)
    prod = [f for f in changed if not repo.is_test(f)]
    every = repo.runners()  # a script is a test by the runners found here, whatever --runner leaves out
    scripts = {f for f in changed if f in repo.ex and not repo.runnable(f) and _node_script(repo, every, f)}
    skipped = []  # (file, why): files named that are no test, and tests no runner here can run
    outside = []  # changed tests outside the scope of the crew lane that runs this
    if a.tests:
        tests = []
        for t in _split_lists(a.tests):
            rel = os.path.relpath(os.path.abspath(t), top).replace("\\", "/")
            if rel in repo.ex and (repo.runnable(rel) or _node_script(repo, every, rel, named=True)):
                tests.append(rel)
            else:
                skipped.append((rel, "not a test file here"))
    else:
        # in a crew lane, the changed tests of its own scope; the whole change is still undone, so they run against
        # a consistent base
        tests, outside = lane_files(top, [f for f in changed if f in repo.ex and (repo.runnable(f) or f in scripts)])
    # test helpers, conftest.py, fixtures: part of the change, restored with it
    helpers = [f for f in changed if repo.is_test(f) and not repo.runnable(f) and f not in scripts and f not in tests]
    # helpers defined inside the changed test modules (outside every test): their fixes are part of the change too
    inner, tangled = {}, []  # tangled: modules whose helpers do not come back as their base text
    reads = {}  # per module, its tests that read a name only the change's helper edit gives (_reads_undone)
    for f in tests:
        restored = _helper_restore(top, base, f)
        if restored is None:
            continue
        if restored["text"] is None:
            tangled.append(f)
        else:
            inner[f] = restored["text"]
            reads[f] = _reads_undone(f, restored)
    if not prod and not helpers and not inner and not tangled:
        return _refuse(f"no production change to prove against: only test modules changed since {label}",
                       "prove checks that each new test fails without your change (code outside the tests, a test "
                       "helper or a fixture); compare with --since <ref>")
    if not tests and not skipped:
        mine = f" in this crew lane's scope ({len(outside)} outside it)" if outside else ""
        return _refuse(f"no new or changed tests since {label}{mine}",
                       "write the test first, or name it: dev test prove <test file>")
    # A test helper the change adds is test code: it stays in the copy, so the new tests that use it run against the
    # old production code. Only when the helpers are the whole change do they go with it.
    at_base = {f: core.git(top, "cat-file", "-e", f"{base}:{f}")[0] == 0 for f in prod + helpers}
    undo = prod + [h for h in helpers if at_base[h] or not prod]
    # the tests that import a file the change adds cannot reach an assertion without it
    added = [f for f in undo if not at_base[f]]
    needs_added = _dependents(repo, added) if added else set()
    plan, unreached = [], []  # unreached: changed test files whose change reaches none of their tests
    for f in tests:
        r = owner(runners, f)
        if r is None:
            skipped.append((f, _no_runner(a, f)))
            continue
        units = _changed_units(top, base, f)  # a named file too: its new or changed tests (all, when none changed)
        if units == []:  # only helpers no test calls changed: a named file runs all its tests, as before
            if not a.tests:
                unreached.append(f)
                continue
            units = None
        plan.append((r, f, units, _unit_ids(top, r, f, units) if units else None))
    if not plan:
        skipped += [(f, "no test of it changed or calls a helper that changed") for f in unreached]
        return _refuse("nothing to prove here: " + "; ".join(f"{f} ({why})" for f, why in skipped),
                       "dev test plan shows the runners found here")
    isolated = bool(getattr(a, "isolated_imports", False))
    if isolated and not all(_direct_unittest(r) for r, _, _, _ in plan):
        return _refuse("--isolated-imports requires direct unittest on the current interpreter",
                       "ordinary dev test prove still reports causal outcomes for other runners")
    groups = {}
    for r, f, units, ids in plan:
        # pytest stops the whole session at a module it cannot import (one that imports a file the change adds): the
        # other tests of the run still run
        g = groups.setdefault(id(r), (dict(r, exec=list(r["exec"]) + ["--continue-on-collection-errors"])
                                      if r["name"] == "pytest" else r, {"files": [], "ids": []}, []))
        (g[1]["ids"].extend(ids) if ids else g[1]["files"].append(f))
        g[2].append((f, units, ids))
    runs = max(1, int(a.runs))
    parent = _tmp_parent()
    parent.mkdir(parents=True, exist_ok=True)
    swept = sweep_prove_copies(parent)
    if swept:
        print(ui.dim(f"removed {len(swept)} copies of proof runs that ended, older than a day "
                     f"({', '.join(swept[:3])}{' …' if len(swept) > 3 else ''})"), file=sys.stderr)
    dest = tempfile.mkdtemp(prefix=f"{PROVE_COPY}{os.getpid()}-", dir=str(parent))
    env = {"PYTHONDONTWRITEBYTECODE": "1"}
    found, problem, gitless, built_stuck = {}, None, "", []
    evidence = {"from_copy": [], "from_checkout": [], "recorded": 0, "unrecorded": [], "py": [], "js": []}
    # Ordinary proof execution is unchanged, but its import environment is not attested for lesson suppression.
    listed = _copy_files(top)  # one list: what the copy holds is what the evidence names, on both sides
    proof_hashes = _proof_snapshot(top, listed) if isolated else {}
    imports_attested = isolated

    def place(res, members):
        hits = []
        for c in res["cases"]:
            for f, units, ids in members:
                if _member(c, f, units, ids):
                    hits.append(((f, c["id"]), c))
                    break
        return hits

    try:
        linked = _copy_worktree(top, dest, listed)
        own = _copy_source_paths(top, dest, linked)
        built, built_stuck = _built_installs(top, dest, linked, listed)
        own += [p for p in built if p not in own]
        if _proof_snapshot(dest, listed) != proof_hashes:
            proof_hashes = {}  # concurrent edits cannot attest to a different snapshot
        if own:  # the copy's src before the editable install's path to the checkout's src
            env["PYTHONPATH"] = os.pathsep.join(own + [p for p in [os.environ.get("PYTHONPATH")] if p])
        gitless = _carry_git(top, dest)
        answered, first = set(), {}  # selected names some case answered to; each runner's first result
        for n in range(runs):
            for r, sel, members in groups.values():
                res = run_tests(dest, r, files=sel["files"], ids=sel["ids"], env=env,
                                **({"isolated_imports": True} if isolated else {}))
                imports_attested = imports_attested and res.get("imports_attested") is True
                if res["zero"]:
                    problem = res
                    break
                first.setdefault(id(r), res)
                for key, c in place(res, members):
                    rec = found.setdefault(key, {"file": key[0], "id": key[1], "name": c.get("name", ""),
                                                 "passes": 0, "skips": 0, "expected_failures": 0, "message": ""})
                    rec["passes"] += c["status"] == "passed"
                    rec["skips"] += c["status"] == "skipped"
                    rec["expected_failures"] += c["status"] == "expected_failure"
                    rec["message"] = rec["message"] or c["message"]
                # a parametrized test answers by its cases (test_x[a], test_x[b]), never by its bare name
                answered |= {(f, _id(i)) for c in res["cases"] for f, units, ids in members for i in ids or ()
                             if _member(c, f, units, [i])}
            if problem:
                break
        for r, sel, members in groups.values():  # a selected name no case answered to did not run
            res = first.get(id(r))
            for f, units, ids in members:
                for i in [i for i in ids or () if res and (f, _id(i)) not in answered]:
                    cover = _cover(res, f, _id(i))
                    found.setdefault((f, _id(i)), {"file": f, "id": _id(i), "passes": 0,
                                                   "message": cover[1] if cover else "did not run"})
        without, by_file = {}, {}  # by_file: a test file's own import evidence, when the run's was incomplete
        if not problem:
            for f, text in inner.items():  # before _restore_base, which clears the copy's bytecode caches
                Path(dest, f).write_text(text)
            _restore_base(top, base, dest, undo)
            recorder, records, recorder_env = _import_recorder(dest, top)
            bare = dict(env, **recorder_env, PYTHONPATH=os.pathsep.join(
                [p for p in [env.get("PYTHONPATH") or os.environ.get("PYTHONPATH"), recorder] if p]))
            for r, sel, members in groups.values():
                res = run_tests(dest, r, files=sel["files"], ids=sel["ids"], env=bare,
                                **({"isolated_imports": True} if isolated else {}))
                imports_attested = imports_attested and res.get("imports_attested") is True
                for key, c in place(res, members):
                    without[key] = (c["status"], c["message"], "load" if c.get("load_failed") else "")
                # a test with no result of its own: only a failure that stopped it before it ran says what happened
                # to it; else it did not run, as far as anyone can tell
                for f, units, ids in members:
                    for key in [k for k in found if k[0] == f and k not in without]:
                        cover = _cover(res, f, key[1])
                        without[key] = ("failed", _tail_line(cover[1]), cover[0]) if cover else ("not run", "", "")
            evidence = _import_evidence(records, top, dest, undo, linked)
            # a Python child that skipped the recorder leaves the run's record incomplete: run each test file again
            # with a record of its own, so only the files that start such a child lose their proof. Only a Python
            # test file outside --isolated-imports has its verdict read from a record: no other file runs again.
            each = [(r, f, ids) for r, _, ms in groups.values() for f, _, ids in ms]
            if evidence["unrecorded"] and len(each) > 1:
                for n, (r, f, ids) in enumerate(each):
                    if _lang(f) != "py" or isolated:
                        continue
                    recorder, records, recorder_env = _import_recorder(dest, top, f"imports-{n}")
                    one = dict(bare, **recorder_env, PYTHONPATH=os.pathsep.join(
                        [p for p in [env.get("PYTHONPATH") or os.environ.get("PYTHONPATH"), recorder] if p]))
                    again = run_tests(dest, r, files=[] if ids else [f], ids=list(ids or []), env=one)
                    by_file[f] = _import_evidence(records, top, dest, undo, linked)
                    if not any(not c.get("load_failed") for c in again["cases"]):
                        # the file did not load this time (the first run left state that stops it): its record says
                        # nothing about what its tests import
                        by_file[f]["recorded"] = 0
    finally:
        shutil.rmtree(dest, ignore_errors=True)
        shutil.rmtree(dest + "-paths", ignore_errors=True)  # the links _copy_mapped_packages made, if any
        for folder in glob.glob(glob.escape(dest) + "-imports*"):  # the import recorders and their records
            shutil.rmtree(folder, ignore_errors=True)
    if problem:
        return _zero_problem(problem, tests)
    rows = []
    py_undo = any(f.endswith(".py") for f in undo)
    runner_of = {f: r for r, f, _, _ in plan}

    def reads_undone(rec):
        bad = sorted(reads.get(rec["file"]) or ())
        return bool(bad) and _member({"id": rec["id"], "file": rec["file"], "name": rec.get("name", "")}, rec["file"],
                                     bad, _unit_ids(top, runner_of[rec["file"]], rec["file"], bad))
    for key, rec in sorted(found.items()):
        st, msg, stop = without.get(key, ("not run", "", ""))
        p = rec["passes"]
        # Neither a skipped assertion nor an expected failure is a passing run of the changed behavior.
        inconclusive = rec.get("skips", 0) + rec.get("expected_failures", 0) == runs
        # A test proves the change only by its own failure: one that stopped it before it ran (its module did not
        # load, its class setup failed) says nothing about its assertions.
        lang = _lang(rec["file"])
        py = lang == "py" and not isolated
        stayed = evidence["py"] if py else evidence["js"] if lang == "js" else []
        mine = by_file.get(rec["file"], evidence)
        verdict = (("expected failure" if rec.get("expected_failures") else "skipped") if inconclusive else
                   "flaky" if 0 < p < runs else
                   "fails with the change" if p == 0 else
                   # its module ran with its helper change in place, or it reads a name only that change gives, so
                   # its result says nothing about the change
                   _HELPERS_TANGLED if rec["file"] in tangled or reads_undone(rec) else
                   # the run without the change ran the checkout's changed code: its result says nothing
                   _CHECKOUT_IMPORT + ", ".join(stayed) if stayed else
                   _NO_IMPORT_RECORD if py and py_undo and (not mine["recorded"] or mine["unrecorded"]) else
                   _IMPORTS_ADDED if st == "failed" and rec["file"] in needs_added and (
                       stop == "load" or _IMPORT_FAILED.search(msg)) else
                   _IMPORT_STOPPED if st == "failed" and (stop == "load" or _stopped_at_import(msg)) else
                   _SETUP_STOPPED if st == "failed" and stop == "setup" else
                   # a failure proves the change only when the changed code it ran came from the copy
                   _NOT_FROM_COPY if st == "failed" and py and py_undo and not mine["from_copy"] else
                   "proves the change" if st == "failed" else
                   "does not test the change" if st == "passed" else "did not run without the change")
        rows.append({"id": rec["id"], "file": rec["file"], "with_change": f"{p}/{runs}", "without_change": st,
                     "verdict": verdict, "message": msg if st == "failed" else rec["message"],
                     # no record of a JavaScript run's imports: only the workspace links were checked
                     **({"imports_checked": "workspace links only"} if lang == "js" else {})})
    # 0: every test that ran proves the change; 1: one does not; 2: none could be proven (skipped, or it imports a
    # file the change adds)
    red = [x for x in rows if not _not_proven(x["verdict"]) and x["verdict"] != "proves the change"]
    rc = 1 if red or not rows else 0 if any(x["verdict"] == "proves the change" for x in rows) else 2
    prevention_eligible = bool(_record_prevention_proofs(top, rows, proof_hashes, base,
                                                        imports_attested=imports_attested))
    if a.json:
        print(json.dumps({**({"prevention_eligible": prevention_eligible} if isolated else {}),
                          "base": label, "production": prod, "helpers": helpers,
                          **({"test_module_helpers": sorted(inner)} if inner else {}),
                          **({"test_module_helpers_kept": tangled} if tangled else {}),
                          "git": f"none: {gitless}" if gitless else "carried", "runs": runs, "tests": rows,
                          "imports": {k: evidence[k] for k in ("from_copy", "from_checkout")},
                          **({"unrecorded": evidence["unrecorded"]} if evidence["unrecorded"] else {}),
                          **({"built_installs": built_stuck} if built_stuck else {}),
                          "skipped": [{"file": f, "why": why} for f, why in skipped],
                          **({"outside_lane": outside} if outside else {}),
                          **({"no_changed_tests": unreached} if unreached else {}), "exit": rc}, indent=1))
        return rc
    change = prod + helpers + [f"{f} (its helpers)" for f in sorted(inner)]
    print(ui.section(f"Prove · {len(rows)} new or changed test(s) · change: "
                     f"{', '.join(change[:3])}{' …' if len(change) > 3 else ''}"
                     f"{f' ({len(helpers) + len(inner)} in the tests)' if helpers or inner else ''} "
                     f"(restored from {label} for the last run)"))
    if gitless:
        print(f"  {ui.WARN} the copy has no git repository ({gitless}): a test that runs git on its checkout fails there")
    for b in built_stuck:
        print(f"  {ui.WARN} {b['environment']} holds a built copy of {b['package']} (not an editable install) that the "
              f"proof copy cannot replace, so its tests run the built code with or without the change and cannot "
              f"prove it: install it editable (uv sync, pip install -e .) or put its source folder on PYTHONPATH")
    ran = [x for x in rows if x["verdict"] not in ("skipped", "expected failure")]
    green = sum(1 for x in ran if x["with_change"] == f"{runs}/{runs}")
    omitted = "".join(f" · {sum(x['verdict'] == v for x in rows)} {label}" for v, label in
                      (("skipped", "skipped"), ("expected failure", "expected failure(s)"))
                      if any(x["verdict"] == v for x in rows))
    mark = (ui.OK if green == len(ran) else ui.FAIL) if ran else ui.WARN
    print(f"  {mark} with the change: {green} of {len(ran)} test(s) passed {runs} of {runs} runs{omitted}")
    if isolated and not prevention_eligible:
        print(f"  {ui.WARN} lesson prevention remains unverified: no causal receipt with attested checkout/stdlib "
              "imports and a current bounded checkout snapshot")
    _outside_note(outside)
    if unreached:
        print(ui.dim(f"  left out, no test of them changed or calls a helper that changed: {', '.join(unreached)}"))
    if skipped:
        print(f"{ui.WARN} not proven, left out because they cannot run here:")
        for f, why in skipped:
            print(f"    {f}  · {why}")
    sections = (("does not test the change", ui.FAIL, "these tests do not test your change (they pass without it):"),
                ("flaky", ui.FAIL, f"flaky with the change (not {runs} of {runs} green):"),
                ("fails with the change", ui.FAIL, "fail with your change present (fix them first):"),
                ("did not run without the change", ui.WARN, "did not run without the change:"),
                ("skipped", ui.WARN, "skipped by their runner, so neither proven nor failing:"),
                ("expected failure", ui.WARN, "expected failures with the change, so not proven:"),
                (_IMPORTS_ADDED, ui.WARN, "cannot be proven: they import a file the change adds, so without the "
                                          "change they stop at the import, before any assertion:"),
                (_IMPORT_STOPPED, ui.WARN, "cannot be proven: without the change they stop at an import or collection "
                                           "error, before any assertion:"),
                (_SETUP_STOPPED, ui.WARN, "cannot be proven: without the change their class or module setup fails, "
                                          "before any assertion:"),
                (_NO_IMPORT_RECORD, ui.WARN, "cannot be proven: the run without the change left no record of the "
                                             "files it imported (a Python started with -I, -S or -E, or with a "
                                             "PYTHONPATH without devkit's recorder, skips the record" +
                 (": " + "; ".join(f"`{c}`" for c in evidence["unrecorded"][:3]) if evidence["unrecorded"] else "") +
                 "):"),
                (_HELPERS_TANGLED, ui.WARN, "cannot be proven: their module's helpers did not come back one by one "
                                            "as their base text, so the run without the change kept the helper "
                                            "change; or they read a name only the change's helper edit gives (an "
                                            "attribute it sets, a parameter it adds), itself or through a helper the "
                                            "change adds, so without the change they fail on that name:"),
                (_NOT_FROM_COPY, ui.WARN, "cannot be proven: the run without the change imported none of the changed "
                                          "Python files from the copy, so nothing shows that it ran the code without "
                                          "the change:"),
                ("proves the change", ui.OK, "fail without the change, as expected:"))
    for stayed, how in ((evidence["py"], "an environment or a .pth that puts the checkout's folder on the path: "
                                         "install the package editable, or point it at the source folder"),
                        (evidence["js"], "node_modules links the workspace package into the checkout: prove the "
                                         "package's own tests, which import it by path")):
        if stayed:
            sections = ((_CHECKOUT_IMPORT + ", ".join(stayed), ui.WARN,
                         f"cannot be proven: without the change they imported the checkout's {', '.join(stayed)}, "
                         f"where the change stays ({how}):"),) + sections
    for verdict, mark, title in sections:
        mine = [x for x in rows if x["verdict"] == verdict]
        if not mine:
            continue
        print(f"{mark} {title}")
        for x in mine:
            extra = f"  passed {x['with_change'].replace('/', ' of ')} runs" if verdict == "flaky" else \
                f"  · {x['message'][:120]}" if x["message"] and verdict != "does not test the change" else ""
            print(f"    {x['file']}{'' if x['id'] == x['file'] else '  ' + x['id']}{extra}")
        if verdict == "proves the change" and any(x.get("imports_checked") for x in mine):
            print(ui.dim("    for JavaScript, prove checks only that no node_modules link resolves a workspace package "
                         "into the checkout: nothing records which files the run without the change imported"))
    if rc == 2:
        print(f"{ui.WARN} nothing proven: no new or changed test here could be proven against the change")
    return rc


# --------------------------------------------------------------------------- dev test audit (testaudit.py)


def _audit_module():
    import importlib
    # AUDIT_MODULE is a constant (the tests swap it for a fake): no user input reaches this import
    return importlib.import_module(f"{__package__}.{AUDIT_MODULE}")  # nosemgrep: non-literal-import


def _audit(a) -> int:
    try:
        mod = _audit_module()
    except ModuleNotFoundError as e:
        if e.name == f"{__package__}.{AUDIT_MODULE}":
            return _refuse(f"dev test audit is not available yet: lib/devkit/{AUDIT_MODULE}.py is missing",
                           "update devkit, or use dev test prove meanwhile")
        return _refuse(f"dev test audit could not load: {e}")
    except Exception as e:  # a broken module must not take `dev test` down with a traceback
        return _refuse(f"dev test audit could not load lib/devkit/{AUDIT_MODULE}.py: {type(e).__name__}: {e}")
    if getattr(a, "audit_placeholder", False):
        from .cli import Parser  # no abbreviated flags, as every dev command
        ap = Parser(prog="dev test audit")
        mod.add_arguments(ap)
        try:
            a = ap.parse_args(a.audit_args or [])
        except SystemExit as e:
            return int(e.code or 0)
    return mod.cmd(a)


# --------------------------------------------------------------------------- CLI


# A subcommand's copy of an option of `dev test` sets it only when written after the subcommand: with a default of its
# own, argparse would reset what `dev test --since REF prove` set before it.
_KEEP = {"default": argparse.SUPPRESS}


def _options(p, paths=False, sub=False):
    keep = _KEEP if sub else {}
    if paths:
        p.add_argument("paths", nargs="*", help="test files, directories or runner ids of this repository (default: "
                                                "failed, then affected)")
    p.add_argument("--failed", action="store_true", help="only the tests that failed last time", **keep)
    p.add_argument("--affected", action="store_true", help="only the tests your changes can affect", **keep)
    p.add_argument("--all", action="store_true", help="the full suite (once, before the push)", **keep)
    p.add_argument("--since", metavar="REF", help="compare with REF instead of the merge-base with origin/<default> "
                   "(or with the release or integration branch this one was cut from)", **keep)
    p.add_argument("--runner", metavar="NAME", help="only this runner: " + ", ".join(RUNNER_LANG), **keep)
    p.add_argument("--json", action="store_true", **keep)
    p.add_argument("-v", "--verbose", action="store_true", help="stream the runner's full output", **keep)
    _listed_option(p, keep)


def _listed_option(p, keep):
    p.add_argument("--listed", action="store_true", help="the files that hold tests are those the runner's own listing "
                   "names (pytest --collect-only, unittest discovery, vitest list, playwright --list), cached; config "
                   "test.listing turns it on for every run", **keep)


def register(add):
    """Add `dev test`. `add(name, fn, help)` returns an argparse subparser; handlers return exit codes."""
    p = add("test", cmd_test, "Tests: last failures first, then what your changes can affect · run · plan · prove · audit")
    _options(p)
    p.add_argument("-p", "--project", help="another project (id or a unique part of its name); put it before the "
                                           "subcommand: dev test -p acme/api plan")
    sp = p.add_subparsers(dest="test_cmd", metavar="run|plan|prove|audit")
    q = sp.add_parser("run", help="run tests: paths, --failed, --affected or --all")
    _options(q, paths=True, sub=True)
    q = sp.add_parser("plan", help="what would run and why (changed file → tests), with the estimate; runs nothing")
    q.add_argument("--all", action="store_true", help="the whole suite: test files, full-run time, slowest files",
                   **_KEEP)
    q.add_argument("--since", metavar="REF", **_KEEP)
    q.add_argument("--runner", metavar="NAME", **_KEEP)
    q.add_argument("--json", action="store_true", **_KEEP)
    _listed_option(q, _KEEP)
    q = sp.add_parser("prove", help="each new or changed test passes N times with the change and fails without it")
    q.add_argument("tests", nargs="*", help="test files (default: the new or changed ones)")
    q.add_argument("--since", metavar="REF", help="the base without your change (default: merge-base with origin, or "
                   "with the release or integration branch this one was cut from)", **_KEEP)
    q.add_argument("--runs", type=int, default=RUNS, metavar="N", help=f"green runs required with the change "
                                                                     f"(default {RUNS})")
    q.add_argument("--isolated-imports", action="store_true",
                   help="unittest only: disable site and inherited Python paths; attest checkout/stdlib imports "
                        "for lesson prevention receipts (checkout root, src and test folders)")
    q.add_argument("--runner", metavar="NAME", **_KEEP)
    q.add_argument("--json", action="store_true", **_KEEP)
    _listed_option(q, _KEEP)
    try:
        mod = _audit_module()
        q = sp.add_parser("audit", help="tests that cannot fail: no or trivial assertions, skips, sleeps")
        mod.add_arguments(q)
    except Exception:  # not shipped yet (or broken): accept any arguments, explain when run
        q = sp.add_parser("audit", help="tests that cannot fail (testaudit.py)", add_help=False, prefix_chars="+")
        q.add_argument("audit_args", nargs="*")
        q.set_defaults(audit_placeholder=True)
    return ["test"]


def cmd_test(a) -> int:
    sub = getattr(a, "test_cmd", None)
    if getattr(a, "project", None):  # another project (id or a unique part of its name): run from its folder
        from . import cli
        os.chdir(cli._resolve(None, a.project)["path"])
    if sub == "audit":
        return _audit(a)
    if getattr(a, "listed", False):
        os.environ["DEVKIT_TEST_LISTING"] = "1"  # every _Repo of this command, and the dev processes it starts
    top = _top()
    if not top:
        return _refuse("not inside a git repository")
    if getattr(a, "runner", None) and a.runner not in RUNNER_LANG:
        return _refuse(f"unknown runner {a.runner}", "one of: " + ", ".join(RUNNER_LANG))
    runners = _runners(top, a)
    if not runners:
        return _refuse("no test runner found here" + (f" for {a.runner}" if getattr(a, "runner", None) else ""),
                       "supported: " + ", ".join(RUNNER_LANG) + " (node --test, from a package.json test script)")
    if sub == "plan":
        return _plan(a, top, runners)
    if sub == "prove":
        return _prove(a, top, runners)
    return _flow(a, top, runners)


# --------------------------------------------------------------------------- CI steps (dev ci --run --resume)

_FAILFAST = {"pytest": {"-x", "--exitfirst", "--maxfail", "--sw", "--stepwise"}, "unittest": {"-f", "--failfast"},
             "jest": {"--bail", "-b"}, "vitest": {"--bail"}, "go": {"-failfast"},
             "phpunit": {"--stop-on-failure", "--stop-on-defect", "--stop-on-error"},
             "pest": {"--stop-on-failure", "--bail"}}
_NARROW = {"pytest": {"-k", "-m", "--lf", "--last-failed", "--ff", "--failed-first", "--deselect", "--co",
                      "--collect-only", "--sw", "--stepwise"},
           "unittest": {"-k", "-p", "--pattern"},
           "jest": {"-t", "--testNamePattern", "-o", "--onlyChanged", "--changedSince", "--shard",
                    "--findRelatedTests", "--listTests", "--watch", "--watchAll"},
           "vitest": {"-t", "--testNamePattern", "--changed", "--shard", "--watch", "related", "list", "bench"},
           "go": {"-run", "-list", "-bench", "-skip"},
           "phpunit": {"--filter", "--group", "--testsuite", "--exclude-group", "--list-tests"},
           "pest": {"--filter", "--group", "--testsuite", "--exclude-group", "--dirty"}}
_PYTEST_VALUE = {"-n", "-p", "--tb", "-c", "--rootdir", "--dist", "--durations", "-o", "--override-ini", "--cov",
                 "--cov-report", "--maxfail", "--junitxml", "--basetemp", "-W", "--log-level", "--timeout", "-r",
                 "--import-mode", "--confcutdir"}
_GO_VALUE = {"-count", "-timeout", "-p", "-parallel", "-tags", "-coverprofile", "-covermode", "-coverpkg", "-cpu",
             "-benchtime", "-shuffle", "-exec", "-o", "-ldflags", "-gcflags", "-mod", "-vet"}
_MVN_VALUE = {"-pl", "--projects", "-P", "--activate-profiles", "-f", "--file", "-s", "--settings", "-T", "--threads",
              "-rf", "--resume-from", "-gs", "--global-settings", "-t", "--toolchains", "-l", "--log-file"}


def _flags(args) -> set:
    return {x.split("=", 1)[0] for x in args if x.startswith("-")}


def _positionals(args, takes_value) -> list:
    out, skip = [], False
    for x in args:
        if skip:
            skip = False
            continue
        if x.startswith("-"):
            skip = x in takes_value
            continue
        out.append(x)
    return out


_SHELL_OPTIONS = {"pipefail", "errexit", "nounset", "xtrace", "verbose"}
_SHELL_WORDS = {"if": "a conditional", "then": "a conditional", "elif": "a conditional", "else": "a conditional",
                "fi": "a conditional", "case": "a conditional", "esac": "a conditional", "for": "a loop",
                "while": "a loop", "until": "a loop", "select": "a loop", "do": "a loop", "done": "a loop",
                "function": "a function", "{": "a command group", "}": "a command group",
                "(": "a subshell", ")": "a subshell"}


def _step_lines(command):
    """A step's text -> its logical lines [(first line number, start offset, end offset, text)], continuations
    joined."""
    out, start, number, buf = [], 0, 1, None
    pos = 0
    for i, ln in enumerate(command.split("\n"), 1):
        end = pos + len(ln)
        if buf is None:
            buf, start, number = "", pos, i
        if ln.endswith("\\") and not ln.endswith("\\\\"):
            buf += ln[:-1] + " "
        else:
            out.append((number, start, end, buf + ln))
            buf = None
        pos = end + 1
    if buf is not None:
        out.append((number, start, pos - 1, buf))
    return out


def _shell_words(text):
    """A line -> its words with comments dropped and operators as words of their own, or None when it does not
    parse."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _harmless(words, cwd, top, shell, env):
    """Whether a line cannot change what a later test command runs; applies its cd, assignments and exports to
    `cwd`, `shell` and `env`. -> (True, new cwd) or (False, cwd)."""
    name = words[0]
    assign = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
    if name in ("echo", "printf", ":", "true"):
        return True, cwd
    if all(assign.fullmatch(w) for w in words):  # NAME=value: a shell variable, exported later or never
        for w in words:
            k, _, v = w.partition("=")
            known = "$" not in v and "~" not in v  # a value the shell expands is not known here
            if k in env or k in os.environ:  # an exported name (PATH, one exported above): the test sees it
                if not known:
                    return False, cwd
                env[k] = v
            shell[k] = v if known else None
        return True, cwd
    if name == "export" and words[1:]:
        for w in words[1:]:
            if assign.fullmatch(w):
                k, _, v = w.partition("=")
                if "~" in v:  # the shell expands it to a home folder
                    return False, cwd
                shell[k] = env[k] = v
            elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", w) and shell.get(w) is not None:
                env[w] = shell[w]
            else:
                return False, cwd
        return True, cwd
    if name == "set" and words[1:]:  # -e, -u, -x, -v and -o pipefail; never -a, which exports every assignment
        rest = list(words[1:])
        while rest:
            flag = rest.pop(0)
            if not re.fullmatch(r"[+-][euxvo]+", flag):
                return False, cwd
            if "o" in flag and (not rest or rest.pop(0) not in _SHELL_OPTIONS):
                return False, cwd
        return True, cwd
    if name == "cd" and len(words) == 2 and not words[1].startswith(("-", "~")):
        new = os.path.normpath(os.path.join(cwd, words[1]))
        rel = os.path.relpath(os.path.realpath(new), os.path.realpath(top))  # a link out of it leaves it too
        return (False, cwd) if rel == ".." or rel.startswith(".." + os.sep) else (True, new)
    return False, cwd


def _segment_runners(words, cwd, top):
    """The test commands a line runs between its operators, a case pattern or a keyword before them dropped."""
    out, segment = [], []
    for w in words + [";"]:
        if w and set(w) <= set(";&|<>()"):
            if segment and segment[0] in _SHELL_WORDS:
                segment = segment[1:]
            if segment:
                r = recognize(shlex.join(segment), cwd, top)
                if r:
                    out.append(r)
            segment = []
        else:
            segment.append(w)
    return out


def step_runner(command, cwd, top):
    """A CI step's command -> (runner or None, why or None): the runner of the one test command it runs, with the
    folder its `cd` lines lead to and the variables it exports, or why devkit re-runs it whole.

    A multi-line step is recognized when exactly one line is a recognizable test command and every other line cannot
    change what that command runs: blank lines, comments, assignments, export, echo or printf, cd, and set -e, -u, -x
    or -o pipefail. Two test commands, a loop, a conditional, a function, a here-document, a command substitution or
    any other command leave it unrecognized, and `why` says which line; a step with no test command at all says
    nothing. A one-line step is `recognize`'s, without a reason."""
    cmd = (command or "").strip()
    if "\n" not in cmd:
        return recognize(cmd, cwd, top), None
    top, cwd = str(top), str(cwd)
    shell, env, tests, refused = {}, {}, [], None
    for number, _, _, text in _step_lines(command):
        words = _shell_words(text)
        if words == []:
            continue
        shown = " ".join(text.split())[:80]
        reason = None
        if words is None:
            reason = f"line {number} does not parse (`{shown}`)"
        elif "<<" in words or "<<-" in words:
            reason = f"a here-document (line {number})"
        elif words[0] in _SHELL_WORDS or words[1:2] == ["()"] or words[1:3] == ["(", ")"]:
            reason = f"{_SHELL_WORDS.get(words[0], 'a function')} (line {number})"
        elif words[0] in ("[", "[[", "test") and ({"&&", "||"} & set(words)):
            reason = f"a conditional (line {number})"
        if reason:
            refused = refused or reason
            tests += [(number, r) for r in _segment_runners(words or [], cwd, top)]
            continue
        if "`" in text or "$(" in text:
            refused = refused or f"line {number} runs a command of its own (`{shown}`)"
            continue
        operators = any(w and set(w) <= set(";&|<>()") for w in words)
        if not operators and words[0] in ("export", "cd") and "$" in text:
            refused = refused or f"line {number} needs the shell to expand a value (`{shown}`)"
            continue
        if not operators:
            ok, new = _harmless(words, cwd, top, shell, env)
            if ok:
                cwd = new
                continue
        found = _segment_runners(words, cwd, top)
        tests += [(number, dict(r, env=dict(env, **r["env"]))) for r in found]  # the exports its line sees
        if found and operators:
            refused = refused or f"line {number} runs the test command with other commands (`{shown}`)"
        elif not found:
            refused = refused or f"line {number} is neither the test command nor a line that cannot change what it " \
                                 f"runs (`{shown}`)"
    if not tests:
        return None, None
    if len(tests) > 1:
        return None, "a multi-line step devkit re-runs whole: two test commands (lines " + \
            ", ".join(str(n) for n, _ in tests) + ")"
    if refused:
        return None, "a multi-line step devkit re-runs whole: " + refused
    return tests[0][1], None


def _unittest_span(command):
    """The (start, end) offsets of the line that runs `-m unittest` in a multi-line step, as step_runner picks it:
    never a comment or a line step_runner takes as harmless (echo, `:`, an assignment), or None."""
    for _, start, end, text in _step_lines(command):
        words = _shell_words(text) or []
        if not words or (not any(w and set(w) <= set(";&|<>()") for w in words)
                         and _harmless(words, ".", ".", {}, {})[0]):
            continue
        if any(words[i:i + 2] == ["-m", "unittest"] for i in range(len(words))):
            return start, end
    return None


def recognize(command, cwd, top):
    """A CI step that is one plain test-runner call -> a runner rooted at the step's directory, or None.

    None for pipelines, `&&` chains, make targets and selectors devkit cannot reproduce (-k, --filter, -run …): such
    a step is re-run whole. `failfast` is set when a failing run may have skipped tests (-x, --bail, a multi-module
    reactor), so only a whole re-run can follow a failure. A multi-line step is `step_runner`'s."""
    cmd = (command or "").strip()
    if "\n" in cmd:
        return step_runner(cmd, cwd, top)[0]
    if not cmd or re.search(r"&&|\|\||[;|`<>&]|\$\(", cmd):
        return None
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return None
    env = {}
    while argv and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]):
        k, _, v = argv.pop(0).partition("=")
        env[k] = v
    if not argv:
        return None
    top, cwd = str(top), str(cwd)
    root = os.path.relpath(cwd, top).replace("\\", "/")
    root = "." if root in ("", ".") else root
    if root.startswith(".."):
        return None

    def mk(name, lang, ex, flags=(), **kw):
        ff = bool(_flags(flags) & _FAILFAST.get(name, set()))
        return {"name": name, "root": root, "lang": lang, "exec": ex, "env": env, "failfast": ff, "scope": [], **kw}

    prefix = []
    if os.path.basename(argv[0]) in ("uv", "poetry", "pipenv", "hatch", "pdm") and argv[1:2] == ["run"]:
        prefix, argv = argv[:2], argv[2:]
        if not argv:
            return None
    exe = os.path.basename(argv[0])
    mod, rest, py = None, [], None
    if re.fullmatch(r"python[\d.]*|py", exe) and argv[1:2] == ["-m"] and len(argv) >= 3:
        mod, rest, py = argv[2], argv[3:], argv[0]
    elif exe in ("pytest", "py.test"):
        mod, rest = "pytest", argv[1:]
    if mod == "unittest":
        args = rest[1:] if rest[:1] == ["discover"] else rest
        valued = {"-s", "--start-directory", "-t", "--top-level-directory"}
        if rest[:1] != ["discover"] and _positionals(rest, valued):
            return None  # named modules: not a suite run
        if _flags(args) & _NARROW["unittest"]:
            return None
        start, top_level, pos = ".", None, _positionals(args, valued)
        for i, x in enumerate(args):
            if x in ("-s", "--start-directory") and i + 1 < len(args):
                start = args[i + 1]
            elif x.startswith("--start-directory="):
                start = x.split("=", 1)[1]
            elif x in ("-t", "--top-level-directory") and i + 1 < len(args):
                top_level = args[i + 1]
            elif x.startswith("--top-level-directory="):
                top_level = x.split("=", 1)[1]
        start = pos[0] if pos and start == "." else start
        if len(pos) > 1 or (top_level is not None and os.path.normpath(top_level) != "."):
            return None  # a pattern, or a top level other than the step's folder: not the whole suite
        start = os.path.normpath(start).replace("\\", "/")
        # -t . runs the start folder as a package (tests.test_x): its __init__.py runs before the tests
        return mk("unittest", "py", prefix + [py or "python", "-m", "unittest"], args, python=py or "python",
                  start=start, package=top_level is not None and start != ".")
    if mod == "pytest":
        if _flags(rest) & _NARROW["pytest"]:
            return None
        pos = _positionals(rest, _PYTEST_VALUE)
        keep, i = [], 0
        while i < len(rest):  # flags stay (minus coverage gates and reports); paths become the selection
            x = rest[i]
            if not x.startswith("-"):
                i += 1
                continue
            val = rest[i + 1:i + 2] if x in _PYTEST_VALUE else []
            if not x.startswith(("--cov", "--junitxml")):
                keep += [x] + val
            i += 1 + len(val)
        ex = prefix + ([py, "-m", "pytest"] if py else [argv[0]]) + keep
        r = mk("pytest", "py", ex, rest, python=py or sys.executable)
        r["scope"] = [os.path.normpath(os.path.join(root, x)).replace("\\", "/") for x in pos]
        return r
    if prefix:
        return None
    # Node: npm/pnpm/yarn test → the package.json script; npx/pnpm exec/yarn/bunx jest|vitest; jest|vitest
    tool, extra = None, []
    if exe in ("npm", "pnpm", "yarn") and argv[1:2] in (["test"], ["run"], ["t"]):
        sub = argv[1:]
        if sub[0] == "run":
            if sub[1:2] != ["test"]:
                return None
            sub = sub[1:]
        extra = [x for x in sub[1:] if x != "--"]
        pkg = core.read_json(os.path.join(cwd, "package.json"), {}) or {}
        script = str((pkg.get("scripts") or {}).get("test") or "")
        if re.search(r"&&|\|\||[;|`<>]|\$\(", script):
            return None
        try:
            words = shlex.split(script)
        except ValueError:
            return None
        if not words or words[0] not in ("jest", "vitest"):
            return None
        tool, extra = words[0], words[1:] + extra
    elif exe in ("npx", "bunx") or (exe in ("pnpm", "yarn") and argv[1:2] in (["exec"], ["jest"], ["vitest"])):
        sub = [x for x in argv[1:] if x not in ("exec", "--no-install", "--")]
        if not sub or sub[0] not in ("jest", "vitest"):
            return None
        tool, extra = sub[0], sub[1:]
    elif exe in ("jest", "vitest"):
        tool, extra = exe, argv[1:]
    if tool:
        if (_flags(extra) | set(extra)) & _NARROW[tool] or (tool == "vitest" and extra[:1] in (["watch"], ["dev"])):
            return None
        return mk(tool, "js", _pm_exec(top, "" if root == "." else root, tool), extra)
    # Maven and Gradle
    if exe in ("mvn", "mvnw"):
        goals = _positionals(argv[1:], _MVN_VALUE)
        if not set(goals) & {"test", "verify", "install", "package"} or any(
                x.startswith(("-Dtest=", "-Dit.test=", "-DskipTests", "-Dmaven.test.skip")) or x in ("-pl", "-rf",
                                                                                                   "--projects")
                for x in argv[1:]):
            return None
        pom = _read(os.path.join(cwd, "pom.xml"))
        r = mk("maven", "jvm", [argv[0]], argv[1:], argv=list(argv))
        r["failfast"] = "<modules>" in pom
        return r
    if exe in ("gradle", "gradlew"):
        tasks = [x for x in argv[1:] if not x.startswith("-")]
        if not tasks or not all(re.fullmatch(r"(:[\w-]+)*:?test", t) for t in tasks) or "--tests" in argv:
            return None
        settings = _read(os.path.join(cwd, "settings.gradle")) + _read(os.path.join(cwd, "settings.gradle.kts"))
        r = mk("gradle", "jvm", [argv[0]] + [x for x in argv[1:] if x.startswith("-")], argv[1:])
        r["failfast"] = bool(re.search(r"\binclude\b", settings)) and "--continue" not in argv
        return r
    if exe == "go" and argv[1:2] == ["test"]:
        args = argv[2:]
        if _flags(args) & _NARROW["go"]:
            return None
        pkgs = _positionals(args, _GO_VALUE)
        d = Path(cwd)
        while not (d / "go.mod").exists() and str(d).startswith(top) and d != d.parent:
            d = d.parent
        m = re.search(r"(?m)^\s*module\s+(\S+)", _read(d / "go.mod"))
        groot = os.path.relpath(str(d), top).replace("\\", "/")
        flags = [x for x in args if x not in pkgs and x not in ("-json", "-v")]
        r = mk("go", "go", ["go", "test"] + flags, args, module=m.group(1) if m else "")
        r["root"] = "." if groot in ("", ".") else groot
        scope = []
        for pk in pkgs or ["."]:
            sub = re.sub(r"/?\.\.\.$", "", re.sub(r"^(?:\./)+", "", pk) if pk.startswith("./") else pk) if pk.startswith(".") \
                else None
            if sub is None:
                return None
            scope.append(os.path.normpath(os.path.join(root, sub or ".")).replace("\\", "/"))
        r["scope"] = [] if scope in (["."], [root]) else scope
        return r
    # PHP
    if exe in ("phpunit", "pest") or (exe == "php" and len(argv) > 1 and os.path.basename(argv[1]) in ("phpunit",
                                                                                                        "pest")):
        binary = argv[:2] if exe == "php" else argv[:1]
        rest = argv[len(binary):]
        name = os.path.basename(binary[-1])
        if _flags(rest) & _NARROW[name] or _positionals(rest, {"-c", "--configuration", "--log-junit"}):
            return None
        return mk(name, "php", binary + rest, rest)
    return None


def step_failures(runner, log, top, since_ts=0):
    """Failed tests named in a CI step's output (or its fresh JUnit reports) -> [{"id", "file"}], or None when the
    failure cannot be pinned on tests (a build error, an unknown format): then the whole step re-runs."""
    top, name, log = str(top), runner["name"], ui.ANSI_RE.sub("", log or "")
    out = []
    if name == "unittest":
        out = [{"id": c["id"], "file": c["file"]} for c in parse_unittest(log, top, runner) if c["status"] == "failed"]
    elif name == "pytest":
        for m in re.finditer(r"(?m)^(?:FAILED|ERROR) (\S+?)(?: - .*)?$", log):
            fpart = m.group(1).split("::")[0]
            out.append({"id": m.group(1), "file": os.path.normpath(os.path.join(runner["root"], fpart)).replace(
                "\\", "/")})
    elif name in ("jest", "vitest"):
        for f in re.findall(r"(?m)^\s*FAIL\s+(\S+\.(?:[cm]?[jt]sx?|vue|svelte))\b", log):
            rel = os.path.normpath(os.path.join(runner["root"], f)).replace("\\", "/")
            out.append({"id": rel, "file": rel})
    elif name == "go":
        if '"Action"' in log:
            out = [{"id": c["id"], "file": c["file"]} for c in parse_go_json(log, top, runner)
                   if c["status"] == "failed"]
        else:
            pending, module = [], runner.get("module") or ""
            for ln in log.splitlines():
                m = re.match(r"^--- FAIL: (\S+)", ln)
                if m:
                    pending.append(m.group(1).split("/")[0])
                    continue
                m = re.match(r"^FAIL\s+(\S+)(?:\s+\[(build|setup) failed\]|\s+[\d.]+s)?\s*$", ln)
                if m and m.group(1) != "FAIL":
                    pkg = m.group(1)
                    sub = pkg[len(module):].lstrip("/") if module and pkg.startswith(module) else pkg
                    d = os.path.normpath(os.path.join(runner["root"], sub or ".")).replace("\\", "/")
                    if pending and not m.group(2):
                        out += [{"id": f"{d}:{t}", "file": d} for t in dict.fromkeys(pending)]
                    else:
                        out.append({"id": d, "file": d})
                    pending = []
    elif name in ("maven", "gradle"):
        out = [{"id": c["id"], "file": c["file"]} for c in _jvm_reports(top, runner, since_ts)
               if c["status"] == "failed"]
    elif name in ("phpunit", "pest"):
        out = [{"id": f"{c}::{m}", "file": ""} for c, m in re.findall(r"(?m)^\d+\) ([\w\\]+)::(\w+)", log)]
    seen, uniq = set(), []
    for f in out:
        if f["id"] not in seen:
            seen.add(f["id"])
            uniq.append(f)
    return uniq or None


def partial(top, runner, failed=(), changed=(), env=None, timeout=None, log=None, stream=False) -> dict:
    """Re-run a CI test step partially: its failed tests plus the tests `changed` can affect.

    -> {"whole": why} (radius "all", or a selection that ran 0 tests) | {"skip": why} (nothing to re-run)
       | {"result": run_tests(...) result}"""
    top = str(top)
    tests = []
    if changed:
        radius = blast_radius(top, changed)
        mine = lambda r: r == "." or _under(runner["root"], r) or _under(r, runner["root"])  # noqa: E731
        if radius["mode"] == "all" and any(mine(r) for r in radius["all_roots"]):
            f, why = radius["reasons"][0]
            return {"whole": f"{f}: {why}"}
        tests = [t for t in radius["tests"] if _lang(t) == runner["lang"] and _under(t, runner["root"])
                 and (not runner.get("scope") or any(_under(t, sc) for sc in runner["scope"]))]
    ids = [f for f in failed or () if f.get("file") not in tests
           and (not f.get("file") or os.path.exists(os.path.join(top, f["file"])))]
    if not tests and not ids:
        return {"skip": "no change since affects its tests"}
    res = run_tests(top, runner, files=tests, ids=ids, env=env, timeout=timeout, log=log, stream=stream)
    if res["zero"]:
        return {"whole": f"the partial selection ran 0 tests ({' '.join(res['argv'])[:120]})"}
    return {"result": res}
