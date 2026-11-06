# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""`dev test`: the tests that failed last time, then the tests a change can affect, and proof that new tests test it.

  dev test                       1) the last failures (stop if one still fails)  2) the blast radius of the changes
  dev test run [paths…]          --failed · --affected · --all · --since REF · --runner NAME · --json
  dev test plan [--all]          what would run, why (changed file → tests), the estimate; runs nothing
  dev test prove [tests…]        each new or changed test passes N times with the change and fails without it
  dev test audit …               tests that cannot fail (testaudit.py)

Blast radius (`blast_radius`): build, dependency, lock, test-config, shared-fixture and CI files mean "all"; docs mean
"none"; a test file selects itself; a source file selects the tests that depend on it through a static graph
(Python imports parsed with `ast`, JS/TS import/require with relative, index and tsconfig-alias resolution,
Java/Kotlin/PHP class-name references, Go package imports), plus the name convention (foo.py ↔ test_foo.py). A file
nothing maps to widens to the tests of its directory or package, up to "all"; it never selects nothing.

Results: JUnit XML (pytest, maven, gradle, phpunit/pest), `go test -json`, jest/vitest JSON and unittest's own output.
A selection that runs zero tests is a selection problem (exit 2), never a pass.

Recorded per project in STATE/tests/<project>.json: the last failures, durations per test file or class, the last
full-run duration, the tree and the time. Python 3.9 compatible, stdlib only.
"""
from __future__ import annotations

import ast
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path

from . import core, ui

TMP_ROOT = None          # parent of `dev test prove` copies; None means core.TMP_ROOT (tests redirect it)
AUDIT_MODULE = "testaudit"
RUNS = 5                 # `dev test prove`: green runs a new test needs with the change present

PY_EXT = {".py"}
JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte"}
JVM_EXT = {".java", ".kt", ".groovy", ".scala"}
LANGS = {**{e: "py" for e in PY_EXT}, **{e: "js" for e in JS_EXT}, **{e: "jvm" for e in JVM_EXT},
         ".go": "go", ".php": "php"}
RUNNER_LANG = {"pytest": "py", "unittest": "py", "jest": "js", "vitest": "js", "maven": "jvm", "gradle": "jvm",
               "go": "go", "phpunit": "php", "pest": "php"}
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
    if _runnable(p):
        return True
    if any(seg in _TEST_DIRS for seg in p.split("/")[:-1]):
        return True
    return bool(re.search(r"(^|/)src/[A-Za-z]*[tT]est[A-Za-z]*/", p))


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
        self.tests = sorted(f for f in self.existing if _runnable(f))
        self._text, self._graphs = {}, {}
        self._runners = None

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


# --------------------------------------------------------------------------- changed files


def base_ref(top, since=None):
    """-> (commit sha or None, label): the merge-base with origin/<default>, or `since`."""
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
                return mb, ref
    return None, "HEAD"


def changed_files(top, since=None) -> list:
    """Files changed against the merge-base with origin/<default> (or `since`), plus staged, unstaged and untracked
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


def _venv_python(top, root):
    for d in (Path(top) / root, Path(top)):
        py = d / ".venv" / "bin" / "python"
        if py.exists():
            return str(py)
    return None


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
        py = _venv_python(top, d) or sys.executable
        if pytest:
            ex = [py, "-m", "pytest"]
            if not _venv_python(top, d) and has(d, "uv.lock") and shutil.which("uv"):
                ex = ["uv", "run", "pytest"]
            found.append({"name": "pytest", "root": rel(d), "lang": "py", "exec": ex, "python": py})
        else:
            start = next((s for s in ("tests", "test") if any(f.startswith((d + "/" if d else "") + s + "/")
                                                             for f in mine)), ".")
            found.append({"name": "unittest", "root": rel(d), "lang": "py", "exec": [py, "-m", "unittest"],
                          "python": py, "start": start})

    # Node: jest or vitest per package.json
    for d in sorted(d for d in by_dir if has(d, "package.json")):
        pkg = core.read_json(os.path.join(absd(d), "package.json"), {}) or {}
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        script = str((pkg.get("scripts") or {}).get("test") or "")
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
        if tool:
            found.append({"name": tool, "root": rel(d), "lang": "js", "exec": _pm_exec(top, d, tool)})

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
    return found


def _under(path: str, root: str) -> bool:
    return root in (".", "") or path == root or path.startswith(root.rstrip("/") + "/")


def owner(runners, path):
    """The runner that runs test file `path`: same language, deepest root."""
    lang = _lang(path)
    cands = [r for r in runners if r["lang"] == lang and _under(path, r["root"])]
    return max(cands, key=lambda r: len(r["root"]) if r["root"] != "." else 0) if cands else None


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
    """No test maps to `p`: the tests of its directory or package, then its parents', up to its runner (all)."""
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


def _one(repo, p):
    """-> (mode, tests, why, scope) for one changed file."""
    g = _global_reason(p)
    d = p.rpartition("/")[0]
    if g and p.endswith(".py") and p.rpartition("/")[2] != "conftest.py" and \
            (d + "/__init__.py" if d else "__init__.py") in repo.all:
        g = None  # setup.py, noxfile.py inside a package: a module like any other
    if g:
        return "all", set(), f"{g}: every test can depend on it", _root_of(repo, p)
    if is_test_file(p):
        if _runnable(p):
            return ("some", {p}, "test file", None) if p in repo.ex else ("some", set(), "deleted test file", None)
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
        sel = {t for t in _dependents(repo, [p]) if t in repo.tests} | _convention(repo, p) | _mentions(repo, p)
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
    env = {}
    if name == "pytest":
        argv = ex + ["-q"] + ([f"--junitxml={report}"] if report else []) + \
            ([] if all_ else rel + [_id(i) for i in ids])
    elif name == "unittest":
        start = runner.get("start") or "."
        sdir = os.path.join(_rootdir(top, runner), start)
        env["PYTHONPATH"] = os.pathsep.join([os.path.normpath(sdir)] + [p for p in [os.environ.get("PYTHONPATH")] if p])
        if all_:
            argv = ex + ["discover", "-v", "-s", start]
        else:
            mods = []
            for r in rel:
                sub = os.path.relpath(r, start).replace("\\", "/") if start != "." else r
                if sub.startswith("../"):
                    sub = r
                mods.append(re.sub(r"\.py$", "", sub).replace("/", "."))
            argv = ex + ["-v"] + mods + [_id(i) for i in ids]
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
    return {"argv": argv, "cwd": _rootdir(top, runner), "env": env, "report": report}


# --------------------------------------------------------------------------- results


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


def parse_junit(text, top, base, runner) -> list:
    """JUnit XML (pytest --junitxml, surefire/failsafe, gradle test-results, phpunit --log-junit) -> cases."""
    text = text.decode("utf-8", "replace") if isinstance(text, bytes) else (text or "")
    if re.search(r"<!(DOCTYPE|ENTITY)", text[:4096], re.I):  # reports never declare entities (XXE, billion laughs)
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
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
        if runner == "pytest":
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
_UT_LINE = re.compile(r"^(\S+) \(([\w.]+)\)(?:[^\n]*?)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|"
                      r"unexpected success)\s*$")
_UT_FRAME = re.compile(r'^\s*File "(.+?)", line (\d+), in ')


def _ut_id(name: str, paren: str) -> str:
    if paren.startswith(("unittest.loader._FailedTest", "unittest.loader.ModuleImportFailure")):
        return name
    if name in ("setUpClass", "tearDownClass", "setUpModule", "tearDownModule"):
        return paren
    return paren if paren.endswith("." + name) else f"{paren}.{name}"


def _ut_file(top, runner, id_: str) -> str:
    base = os.path.join(_rootdir(top, runner), runner.get("start") or ".")
    parts = id_.split(".")
    for i in range(len(parts), 0, -1):
        cand = os.path.join(base, *parts[:i]) + ".py"
        if os.path.exists(cand):
            return _relto(top, cand)
    return ""


def parse_unittest(text, top, runner) -> list:
    """unittest's verbose output (3.9 and 3.11+ formats) -> cases, failures with file:line and the error line."""
    lines = (text or "").splitlines()
    cases = {}
    for ln in lines:
        m = _UT_LINE.match(ln)
        if m:
            id_ = _ut_id(m.group(1), m.group(2))
            st = m.group(3)
            status = "passed" if st in ("ok", "expected failure") else "skipped" if st.startswith("skipped") \
                else "failed"
            cases[id_] = _case(id_, _ut_file(top, runner, id_), None, status, "", 0, m.group(1))
    i = 0
    while i < len(lines):
        m = _UT_HEAD.match(lines[i])
        if not m:
            i += 1
            continue
        id_ = _ut_id(m.group(2), m.group(3))
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
    if name in ("jest", "vitest"):
        return parse_jest_json(_read(report), top) if report and os.path.exists(report) else []
    if name == "unittest":
        return parse_unittest(output, top, runner)
    if name == "go":
        return parse_go_json(output, top, runner)
    return _jvm_reports(top, runner, t0)


def _tail_line(output: str) -> str:
    lines = [ui.ANSI_RE.sub("", ln).strip() for ln in (output or "").splitlines()]
    lines = [ln for ln in lines if ln and not re.fullmatch(r"[=\-_*~. ]+", ln)]
    errs = [ln for ln in lines[-40:] if re.search(r"(?i)(error|exception|not found|no such|cannot|failed)", ln)]
    return (errs[0] if errs else lines[-1] if lines else "")[:300]


def _tmp_parent() -> Path:
    return Path(TMP_ROOT or core.TMP_ROOT)


def run_tests(top, runner, files=(), ids=(), related=(), all_=False, env=None, stream=False, log=None,
              timeout=None) -> dict:
    """Run one selection with one runner; stream the output when asked; parse the results.

    -> {runner, root, files, ids, related, all, argv, rc, secs, cases, count, passed, failed, skipped,
        failures, zero (the selection ran no test), crash (no usable result), log}"""
    top = str(top)
    files, ids, related = list(files), list(ids), list(related)
    tmpd = core.STATE / "tests" / "tmp"
    tmpd.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=str(tmpd)))
    report = None
    if runner["name"] in ("pytest", "phpunit", "pest"):
        report = str(work / "junit.xml")
    elif runner["name"] in ("jest", "vitest"):
        report = str(work / "report.json")
    cmd = build_command(top, runner, files, ids, report, related, all_)
    full_env = dict(os.environ)
    full_env.update(env or {})
    for k, v in cmd["env"].items():
        full_env[k] = v if k != "PYTHONPATH" or not (env or {}).get(k) else v + os.pathsep + env[k]
    log = Path(log) if log else work / "output.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    out, t0 = [], time.time()
    try:
        # nosemgrep: python36-compatibility-Popen1 (devkit needs Python 3.9+, where Popen takes errors=)
        with open(log, "w") as fh, subprocess.Popen([str(x) for x in cmd["argv"]], cwd=cmd["cwd"], env=full_env,
                                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                                   errors="replace") as proc:
            timer = None
            if timeout:
                import threading
                timer = threading.Timer(timeout, proc.kill)
                timer.start()
            for ln in proc.stdout:
                out.append(ln)
                fh.write(ln)
                if stream:
                    sys.stdout.write(ln)
                    sys.stdout.flush()
            rc = proc.wait()
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
    cases = list(cases.values())
    shutil.rmtree(work, ignore_errors=True)
    failures = [c for c in cases if c["status"] == "failed"]
    zero = not cases and (rc == 0 or rc == 5 or bool(_NO_TESTS.search(text)))
    crash = False
    if not cases and not zero:
        crash = True
        units = ids or files or [runner["name"]]
        failures = [_case(_id(u), (u.get("file") if isinstance(u, dict) else u if u in files else ""), None,
                          "failed", f"{runner['name']} exited {rc}: {_tail_line(text)}") for u in units]
    elif rc != 0 and not failures:
        crash = True
        failures = [_case(f"{runner['name']}:{runner['root']}", "", None, "failed",
                          f"{runner['name']} exited {rc} after {len(cases)} test(s): {_tail_line(text)}")]
    count = len(cases)
    return {"runner": runner["name"], "root": runner["root"], "files": files, "ids": [_id(i) for i in ids],
            "related": related, "all": bool(all_), "argv": [str(x) for x in cmd["argv"]], "cwd": cmd["cwd"],
            "rc": rc, "secs": round(secs, 2), "cases": cases, "count": count,
            "passed": sum(c["status"] == "passed" for c in cases), "failed": len(failures),
            "skipped": sum(c["status"] == "skipped" for c in cases), "failures": failures, "zero": zero,
            "crash": crash, "log": str(log) if log.parent != work else ""}


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


def record(top, results, kind="run") -> dict:
    """Fold run results into the project's record: failures replaced for what ran, durations, full-run time."""
    top = str(top)
    path = state_path(top)
    st = load_state(top)
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
        keep = []
        for f in fails:
            same = f.get("runner") == res["runner"] and f.get("root") == res["root"]
            if same and (res["all"] or f.get("id") in ran_ids or (not res["ids"] and f.get("file") in ran_files)):
                continue
            keep.append(f)
        fails = keep + [{"runner": res["runner"], "root": res["root"], "id": c["id"], "file": c["file"],
                         "line": c["line"], "message": c["message"], "ts": now} for c in res["failures"]]
        rk = f"{res['runner']}:{res['root']}"
        durs = st.setdefault("durations", {}).setdefault(rk, {})
        per = {}
        for c in res["cases"]:
            if c["file"]:
                per[c["file"]] = per.get(c["file"], 0) + c["secs"]
        if per and sum(per.values()) > 0:
            durs.update({k: round(v, 3) for k, v in per.items()})
        elif res["files"] and not res["ids"] and not res["crash"]:
            durs.update({f: round(res["secs"] / len(res["files"]), 3) for f in res["files"]})
        if res["all"] and not res["crash"]:
            st.setdefault("full", {})[rk] = {"secs": res["secs"], "count": res["count"], "tree": tree, "ts": now}
    st.setdefault("failures", {})[top] = fails
    st["last"] = {"top": top, "kind": kind, "tree": tree, "ts": now, "count": sum(r["count"] for r in results),
                  "failed": sum(r["failed"] for r in results), "secs": round(sum(r["secs"] for r in results), 2)}
    st["tree"], st["ts"] = tree, now
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    os.replace(tmp, path)
    return st


def _forget(top, stale) -> None:
    """Drop recorded failures that no longer select a test (renamed or deleted)."""
    st = load_state(top)
    fails = (st.get("failures") or {}).get(str(top)) or []
    st.setdefault("failures", {})[str(top)] = [f for f in fails if (f.get("runner"), f.get("root"), f.get("id"))
                                               not in stale]
    p = state_path(top)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=1))


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
    counts = f"{res['count']} test(s)" + (f", {res['failed']} failed" if res["failed"] else "")
    print(f"  {ui.OK if ok else ui.FAIL} {res['runner']} · {_label(res)} · {counts} · {_fmt(res['secs'])}")
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
        if set(files) >= every.get(id(r), {None}):  # the selection is the whole suite: run it as one (timed)
            out.append((r, {"all": True, "files": [], "related": []}))
            continue
        rel = []
        if r["name"] in ("jest", "vitest") and _has_bin(top, r, r["name"]):
            rel = [f for f in changed if _lang(f) == "js" and _under(f, r["root"]) and os.path.exists(
                os.path.join(top, f))]
        out.append((r, {"files": [] if rel else sorted(files), "related": rel}))
    return out, orphans


def _failed_groups(top, runners, st):
    fails = [f for f in (st.get("failures") or {}).get(top) or []
             if not f.get("file") or os.path.exists(os.path.join(top, f["file"]))]
    out = []
    for r in runners:
        mine = [f for f in fails if f.get("runner") == r["name"] and f.get("root") == r["root"]]
        if mine:
            out.append((r, mine))
    return out


def _run_step(top, groups, stream, json_out, kind, header):
    if not json_out:
        print(ui.section(header))
    results = []
    for r, sel in groups:
        res = run_tests(top, r, files=sel.get("files", ()), ids=sel.get("ids", ()), related=sel.get("related", ()),
                        all_=sel.get("all", False), stream=stream, log=_log_path(top, r))
        results.append(res)
        if not json_out:
            _show(res, top)
    return {"kind": kind, "runs": results}


def _log_path(top, runner) -> Path:
    key = project_key(top)[1]
    root = re.sub(r"[^A-Za-z0-9._-]+", "_", runner["root"])
    return core.STATE / "tests" / "logs" / f"{key}-{runner['name']}-{root}.log"


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
        groups = _failed_groups(top, runners, st)
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
                print(f"{ui.WARN} {sum(len(r['ids']) for r in stale)} recorded failure(s) no longer select a test "
                      f"(renamed or deleted): forgotten", file=sys.stderr)
            if live:
                record(top, live, "failed")
            if any(r["failures"] for r in live):
                if not json_out:
                    n = sum(r["failed"] for r in live)
                    print(f"{ui.FAIL} {n} test(s) still fail: fix them, then `dev test` again (failures run first)")
                return finish(1)
            if only_failed:
                return finish(_verdict(live, json_out, "failed"))

    changed = changed_files(top, a.since)
    radius = blast_radius(top, changed)
    data["radius"] = radius
    if radius["mode"] == "none" or (not radius["tests"] and radius["mode"] != "all"):
        if not json_out:
            why = "; ".join(f"{f}: {w}" for f, w in radius["reasons"][:4]) or "no changes"
            print(f"{ui.OK} nothing to run for the changes: {why}")
        return finish(code)
    groups, orphans = _group_tests(top, runners, radius["tests"], changed)
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
        print(f"{ui.OK} {count} test(s) passed{tail}")
    return 0


def _explicit(top, runners, paths):
    """Paths (files, dirs, ids) relative to the cwd -> runner groups, plus what no runner takes."""
    groups, bad = {}, []
    tests = _Repo(top).tests
    for raw in paths:
        file_part, sep, rest = raw.partition("::")
        absf = os.path.abspath(file_part)
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
    changed = changed_files(top, a.since)
    radius = blast_radius(top, changed)
    failed = _failed_groups(top, runners, st)
    groups, orphans = _group_tests(top, runners, radius["tests"], changed)
    runs = []
    for r, sel in groups:
        n_files = sum(1 for t in repo.tests if owner(runners, t) is r)
        cmd = build_command(top, r, files=sel["files"], related=sel["related"], report="<report>",
                            all_=sel.get("all", False))
        runs.append({"runner": r["name"], "root": r["root"], "files": sel["files"], "related": sel["related"],
                     "all": sel.get("all", False), "argv": cmd["argv"],
                     "estimate": estimate(st, r, all_=True) if sel.get("all") else
                     estimate(st, r, sel["files"], n_files=n_files)})
    ests = [x["estimate"] for x in runs]
    total = None if any(e is None for e in ests) else round(sum(ests), 1)
    full = [estimate(st, r, all_=True) for r in runners]
    full_total = None if not full or any(e is None for e in full) else round(sum(full), 1)
    if a.json:
        print(json.dumps({"project": pid, "base": label, "changed": changed, "radius": radius,
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
    if orphans:
        print(f"  {ui.WARN} no runner for: {', '.join(orphans[:5])}")
    if radius["mode"] == "all":
        print(f"  {ui.WARN} the changes can affect every test (see above); `dev test` runs the selection above "
              f"and says so · full suite ~{_fmt(full_total)}: dev test --all, or dev ci --run before the push")
    elif not runs:
        print("  nothing to run")
    else:
        print(f"  estimate: ~{_fmt(total)} · full suite: ~{_fmt(full_total)}")
    return 0


# --------------------------------------------------------------------------- dev test prove

_JS_UNIT = re.compile(r"^\s*(?:it|test)(?:\.(?:only|skip|concurrent|todo|failing))?\s*\(\s*(['\"`])(.+?)\1", re.M)
_GO_UNIT = re.compile(r"^func (Test\w+)\s*\(", re.M)
_JVM_UNIT = re.compile(r"@(?:Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b[^\n]*\n(?:\s*@[^\n]*\n)*"
                       r"\s*(?:(?:public|private|protected|internal|open|override|suspend|static|final)\s+)*"
                       r"(?:fun\s+|void\s+|[\w<>\[\],]+\s+)?`?(\w+)`?\s*\(", re.M)
_PHP_UNIT = re.compile(r"(?:(?:@test|#\[Test\])[^\n]*\n(?:[^\n]*\n){0,3}?\s*(?:public\s+)?function\s+(\w+)"
                       r"|function\s+(test\w*)\s*\()", re.M)


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

        out = []
        for node in tree.body:
            if isinstance(node, fn) and node.name.startswith("test"):
                out.append((node.name,) + span(node))
            elif isinstance(node, ast.ClassDef):
                mine = [(f"{node.name}.{i.name}",) + span(i) for i in node.body
                        if isinstance(i, fn) and i.name.startswith("test")]
                if mine:
                    out += mine + [(f"{node.name}.*",) + span(node)]
        return out
    rx = {"js": _JS_UNIT, "go": _GO_UNIT, "jvm": _JVM_UNIT, "php": _PHP_UNIT}.get(lang)
    if rx is None:
        return []
    starts = []
    for m in rx.finditer(text):
        name = m.group(2) if lang == "js" else next((g for g in m.groups() if g), None)
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


def _changed_units(top, base, f):
    """Names of the tests in `f` that are new or changed since `base`; None when the whole file counts."""
    if core.git(top, "cat-file", "-e", f"{base}:{f}")[0] != 0:
        return None
    text = _read(os.path.join(top, f))
    src = text.splitlines()
    lines = {ln for ln in _changed_lines(top, base, f)  # blank and comment-only lines change no test
             if 0 < ln <= len(src) and src[ln - 1].strip() and not src[ln - 1].strip().startswith(("#", "//"))}
    units = _units(f, text)
    if not lines or not units:
        return None
    tests = [(n, s, e) for n, s, e in units if not n.endswith(".*")]
    picked = {n for n, s, e in tests if any(s <= ln <= e for ln in lines)}
    for n, s, e in units:  # a change inside a class but outside its tests (setUp, a helper): all its tests
        if n.endswith(".*"):
            inside = {ln for ln in lines if s <= ln <= e}
            mine = [t for t in tests if t[0].startswith(n[:-1])]
            if inside - {ln for _, ts, te in mine for ln in range(ts, te + 1)}:
                picked |= {t[0] for t in mine}
    return sorted(picked) or None


def _ut_module(runner, rel) -> str:
    start = runner.get("start") or "."
    sub = os.path.relpath(rel, start).replace("\\", "/") if start != "." else rel
    return re.sub(r"\.py$", "", rel if sub.startswith("../") else sub).replace("/", ".")


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
    same = case["file"] == f or (f.endswith(".go") and case["file"].startswith((os.path.dirname(f) or ".") + "/"))
    return same and (units is None or any(case["name"].endswith(u) for u in units))


def _copy_worktree(top, dest) -> None:
    """HEAD plus uncommitted changes (tracked and untracked, not ignored); dependency folders are linked."""
    listed = _ls(top, "-co", "--exclude-standard")
    for f in dict.fromkeys(listed):
        src, dst = os.path.join(top, f), os.path.join(dest, f)
        if not os.path.lexists(src):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
        elif os.path.isfile(src):
            shutil.copy2(src, dst)
    roots = {""} | {f.rpartition("/")[0] for f in listed if f.rpartition("/")[2] in (
        "package.json", "composer.json", "pyproject.toml", "go.mod", "setup.py", "requirements.txt")}
    for d in roots:
        for dep in DEP_DIRS:
            real = os.path.join(top, d, dep)
            link = os.path.join(dest, d, dep)
            if os.path.isdir(real) and not os.path.lexists(link):
                os.makedirs(os.path.dirname(link), exist_ok=True)
                os.symlink(real, link)


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
        subs[:] = [s for s in subs if s not in DEP_DIRS]
        if "__pycache__" in subs:
            shutil.rmtree(os.path.join(d, "__pycache__"), ignore_errors=True)
            subs.remove("__pycache__")


def _prove(a, top, runners) -> int:
    base, label = base_ref(top, a.since)
    if not base:
        return _refuse("nothing to compare with: no origin/<default> and no --since", "dev test prove --since <ref>")
    changed = changed_files(top, a.since)
    repo = _Repo(top)
    prod = [f for f in changed if not is_test_file(f)]
    if a.tests:
        tests = []
        for t in a.tests:
            rel = os.path.relpath(os.path.abspath(t), top).replace("\\", "/")
            if rel not in repo.ex or not _runnable(rel):
                return _refuse(f"not a test file here: {t}")
            tests.append(rel)
    else:
        tests = [f for f in changed if _runnable(f) and f in repo.ex]
    if not prod:
        return _refuse(f"no production change to prove against: only tests changed since {label}",
                       "prove checks that each new test fails without your code change; compare with --since <ref>")
    if not tests:
        return _refuse(f"no new or changed tests since {label}",
                       "write the test first, or name it: dev test prove <test file>")
    plan = []
    for f in tests:
        r = owner(runners, f)
        if r is None:
            return _refuse(f"no test runner for {f}", "dev test plan shows the runners found here")
        units = None if a.tests else _changed_units(top, base, f)
        plan.append((r, f, units, _unit_ids(top, r, f, units) if units else None))
    groups = {}
    for r, f, units, ids in plan:
        g = groups.setdefault(id(r), (r, {"files": [], "ids": []}, []))
        (g[1]["ids"].extend(ids) if ids else g[1]["files"].append(f))
        g[2].append((f, units, ids))
    runs = max(1, int(a.runs))
    parent = _tmp_parent()
    parent.mkdir(parents=True, exist_ok=True)
    dest = tempfile.mkdtemp(prefix="devkit-prove-", dir=str(parent))
    env = {"PYTHONDONTWRITEBYTECODE": "1"}
    found, problem = {}, None

    def place(res, members):
        hits = []
        for c in res["cases"]:
            for f, units, ids in members:
                if _member(c, f, units, ids):
                    hits.append(((f, c["id"]), c))
                    break
        return hits

    def unmatched_failed(res, f, ids):
        if res["crash"]:
            return True
        return any(c["status"] == "failed" and (c["file"] == f or any(_id(i).startswith(c["id"] + ".")
                                                                       for i in ids or ()))
                   for c in res["cases"])

    try:
        _copy_worktree(top, dest)
        for n in range(runs):
            for r, sel, members in groups.values():
                res = run_tests(dest, r, files=sel["files"], ids=sel["ids"], env=env)
                if res["zero"]:
                    problem = res
                    break
                for key, c in place(res, members):
                    rec = found.setdefault(key, {"file": key[0], "id": key[1], "passes": 0, "message": ""})
                    rec["passes"] += c["status"] == "passed"
                    rec["message"] = rec["message"] or c["message"]
                for f, units, ids in members:
                    for i in ids or ():
                        found.setdefault((f, _id(i)), {"file": f, "id": _id(i), "passes": 0,
                                                       "message": res["failures"][0]["message"]
                                                       if res["failures"] else "did not run"})
            if problem:
                break
        without = {}
        if not problem:
            _restore_base(top, base, dest, prod)
            for r, sel, members in groups.values():
                res = run_tests(dest, r, files=sel["files"], ids=sel["ids"], env=env)
                for key, c in place(res, members):
                    without[key] = (c["status"], c["message"])
                for f, units, ids in members:
                    for key in [k for k in found if k[0] == f and k not in without]:
                        without[key] = ("failed", _tail_line(res["failures"][0]["message"]) if res["failures"]
                                        else "") if unmatched_failed(res, f, ids) else ("not run", "")
    finally:
        shutil.rmtree(dest, ignore_errors=True)
    if problem:
        return _zero_problem(problem, tests)
    rows = []
    for key, rec in sorted(found.items()):
        st, msg = without.get(key, ("not run", ""))
        p = rec["passes"]
        verdict = ("flaky" if 0 < p < runs else "fails with the change" if p == 0 else
                   "proves the change" if st == "failed" else
                   "does not test the change" if st == "passed" else "did not run without the change")
        rows.append({"id": rec["id"], "file": rec["file"], "with_change": f"{p}/{runs}", "without_change": st,
                     "verdict": verdict, "message": msg if st == "failed" else rec["message"]})
    rc = 0 if rows and all(x["verdict"] == "proves the change" for x in rows) else 1
    if a.json:
        print(json.dumps({"base": label, "production": prod, "runs": runs, "tests": rows, "exit": rc}, indent=1))
        return rc
    print(ui.section(f"Prove · {len(rows)} new or changed test(s) · production change: "
                     f"{', '.join(prod[:3])}{' …' if len(prod) > 3 else ''} (restored from {label} for the last run)"))
    green = sum(1 for x in rows if x["with_change"] == f"{runs}/{runs}")
    print(f"  {ui.OK if green == len(rows) else ui.FAIL} with the change: {green} of {len(rows)} test(s) passed "
          f"{runs} of {runs} runs")
    sections = (("does not test the change", ui.FAIL, "these tests do not test your change (they pass without it):"),
                ("flaky", ui.FAIL, f"flaky with the change (not {runs} of {runs} green):"),
                ("fails with the change", ui.FAIL, "fail with your change present (fix them first):"),
                ("did not run without the change", ui.WARN, "did not run without the change:"),
                ("proves the change", ui.OK, "fail without the change, as expected:"))
    for verdict, mark, title in sections:
        mine = [x for x in rows if x["verdict"] == verdict]
        if not mine:
            continue
        print(f"{mark} {title}")
        for x in mine:
            extra = f"  passed {x['with_change'].replace('/', ' of ')} runs" if verdict == "flaky" else \
                f"  · {x['message'][:120]}" if x["message"] and verdict != "does not test the change" else ""
            print(f"    {x['file']}  {x['id']}{extra}")
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
        import argparse
        ap = argparse.ArgumentParser(prog="dev test audit")
        mod.add_arguments(ap)
        try:
            a = ap.parse_args(a.audit_args or [])
        except SystemExit as e:
            return int(e.code or 0)
    return mod.cmd(a)


# --------------------------------------------------------------------------- CLI


def _options(p, paths=False):
    if paths:
        p.add_argument("paths", nargs="*", help="test files, directories or runner ids (default: failed, then affected)")
    p.add_argument("--failed", action="store_true", help="only the tests that failed last time")
    p.add_argument("--affected", action="store_true", help="only the tests your changes can affect")
    p.add_argument("--all", action="store_true", help="the full suite (once, before the push)")
    p.add_argument("--since", metavar="REF", help="compare with REF instead of the merge-base with origin/<default>")
    p.add_argument("--runner", metavar="NAME", help="only this runner: " + ", ".join(RUNNER_LANG))
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true", help="stream the runner's full output")


def register(add):
    """Add `dev test`. `add(name, fn, help)` returns an argparse subparser; handlers return exit codes."""
    p = add("test", cmd_test, "Tests: last failures first, then what your changes can affect · run · plan · prove · audit")
    _options(p)
    p.add_argument("-p", "--project", help="another project (id or a unique part of its name); put it before the "
                                           "subcommand: dev test -p acme/api plan")
    sp = p.add_subparsers(dest="test_cmd", metavar="run|plan|prove|audit")
    q = sp.add_parser("run", help="run tests: paths, --failed, --affected or --all")
    _options(q, paths=True)
    q = sp.add_parser("plan", help="what would run and why (changed file → tests), with the estimate; runs nothing")
    q.add_argument("--all", action="store_true", help="the whole suite: test files, full-run time, slowest files")
    q.add_argument("--since", metavar="REF")
    q.add_argument("--runner", metavar="NAME")
    q.add_argument("--json", action="store_true")
    q = sp.add_parser("prove", help="each new or changed test passes N times with the change and fails without it")
    q.add_argument("tests", nargs="*", help="test files (default: the new or changed ones)")
    q.add_argument("--since", metavar="REF", help="the base without your change (default: merge-base with origin)")
    q.add_argument("--runs", type=int, default=RUNS, metavar="N", help=f"green runs required with the change "
                                                                     f"(default {RUNS})")
    q.add_argument("--runner", metavar="NAME")
    q.add_argument("--json", action="store_true")
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
        os.chdir(cli._resolve(cli._reg(prs=False), a.project)["path"])
    if sub == "audit":
        return _audit(a)
    top = _top()
    if not top:
        return _refuse("not inside a git repository")
    if getattr(a, "runner", None) and a.runner not in RUNNER_LANG:
        return _refuse(f"unknown runner {a.runner}", "one of: " + ", ".join(RUNNER_LANG))
    runners = _runners(top, a)
    if not runners:
        return _refuse("no test runner found here" + (f" for {a.runner}" if getattr(a, "runner", None) else ""),
                       "supported: pytest, unittest, jest, vitest, maven, gradle, go, phpunit, pest")
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
           "unittest": {"-k", "-p", "--pattern", "-t", "--top-level-directory"},
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


def recognize(command, cwd, top):
    """A CI step that is one plain test-runner call -> a runner rooted at the step's directory, or None.

    None for pipelines, `&&` chains, make targets and selectors devkit cannot reproduce (-k, --filter, -run …): such
    a step is re-run whole. `failfast` is set when a failing run may have skipped tests (-x, --bail, a multi-module
    reactor), so only a whole re-run can follow a failure."""
    cmd = (command or "").strip()
    if not cmd or "\n" in cmd or re.search(r"&&|\|\||[;|`<>&]|\$\(", cmd):
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
        if rest[:1] != ["discover"] and _positionals(rest, {"-s", "--start-directory"}):
            return None  # named modules: not a suite run
        if _flags(args) & _NARROW["unittest"]:
            return None
        start, pos = ".", _positionals(args, {"-s", "--start-directory"})
        for i, x in enumerate(args):
            if x in ("-s", "--start-directory") and i + 1 < len(args):
                start = args[i + 1]
            elif x.startswith("--start-directory="):
                start = x.split("=", 1)[1]
        start = pos[0] if pos and start == "." else start
        if len(pos) > 1:
            return None
        return mk("unittest", "py", prefix + [py or "python", "-m", "unittest"], args, python=py or "python",
                  start=os.path.normpath(start).replace("\\", "/"))
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
