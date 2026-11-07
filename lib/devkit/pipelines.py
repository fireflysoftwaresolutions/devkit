# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev ci audit` and `dev ci init`: diagnose, fix and set up CI/CD pipelines on any of the four big hosts.

  GitHub Actions      .github/workflows/*.y*ml
  GitLab CI           .gitlab-ci.yml and its local includes
  Bitbucket Pipelines bitbucket-pipelines.yml
  Azure Pipelines     azure-pipelines.yml, *.azure-pipelines.yml, .azure-pipelines/** (and local templates)

audit(root)   findings {provider, file, line, id, category, severity, message, fix, autofix} in five categories:
              speed, reliability, security, quality and delivery; merged with actionlint, zizmor and `glab ci lint`
              when they are installed (and glab is signed in)
fix(root)     safe text edits on GitHub workflows that keep comments and layout: least-privilege permissions, job
              timeouts, cancel superseded runs, actions pinned to the commit of their tag
init(root)    a pipeline for the detected stack from templates/ci/<provider>/, pinned from templates/ci/pins.json,
              that audits clean

The YAML reader is a small, dependency-free subset parser (block and flow collections, block scalars, anchors,
aliases and merge keys, GitLab's !reference) that remembers the line of every key, so findings point at the line.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import json
import os
import re
import textwrap
from pathlib import Path

from . import core

TEMPLATES = core.DEVKIT_HOME / "templates" / "ci"
SEVERITIES = ("critical", "high", "medium", "low", "info")
RANK = {s: i for i, s in enumerate(SEVERITIES)}
CATEGORIES = ("security", "reliability", "speed", "quality", "delivery")
PROVIDERS = ("github", "gitlab", "bitbucket", "azure")
PROVIDER_NAMES = {"github": "GitHub Actions", "gitlab": "GitLab CI", "bitbucket": "Bitbucket Pipelines",
                  "azure": "Azure Pipelines"}
SKIP_DIRS = {"node_modules", ".venv", "venv", "vendor", "dist", "build", "target", ".git", ".worktrees", ".next",
             ".tox", ".mypy_cache", ".gradle", "__pycache__", ".cache", "coverage"}


# =========================================================================== YAML subset parser

class YamlError(ValueError):
    def __init__(self, msg: str, line: int):
        super().__init__(f"line {line}: {msg}")
        self.msg, self.line = msg, line


class YMap(dict):
    """A mapping that remembers the 1-based line of each key (`klines`) and of itself (`line`)."""

    def __init__(self, line: int = 0):
        super().__init__()
        self.line = line
        self.klines = {}


class YSeq(list):
    """A sequence that remembers the line of each item (`ilines`)."""

    def __init__(self, line: int = 0):
        super().__init__()
        self.line = line
        self.ilines = []


class YTag:
    """A node with a custom tag, such as GitLab's `!reference [.job, script]`."""

    def __init__(self, tag: str, value, line: int):
        self.tag, self.value, self.line = tag, value, line

    def __repr__(self):
        return f"YTag({self.tag!r}, {self.value!r})"


_INT_RE = re.compile(r"^[-+]?\d+$")


def _scalar(s: str):
    """Resolve a plain scalar the way YAML 1.2's core schema does, except floats, which stay text (3.10 is not 3.1)."""
    if s in ("", "~", "null", "Null", "NULL"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if _INT_RE.match(s) and not (len(s.lstrip("+-")) > 1 and s.lstrip("+-").startswith("0")):
        return int(s)
    return s


def _plain_cut(s: str) -> str:
    """A plain scalar without its trailing comment (` #` starts a comment)."""
    m = re.search(r"(^|[ \t])#", s)
    return (s[:m.start()] if m else s).rstrip()


def _qend(s: str, start: int):
    """Index of the quote that closes the quoted scalar opening at `start`, or None."""
    q = s[start]
    i = start + 1
    while i < len(s):
        c = s[i]
        if q == "'" and c == "'":
            if i + 1 < len(s) and s[i + 1] == "'":
                i += 2
                continue
            return i
        if q == '"':
            if c == "\\":
                i += 2
                continue
            if c == '"':
                return i
        i += 1
    return None


_ESC = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"', "\\": "\\", "/": "/", " ": " ", "e": "\x1b"}


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        out, i, body = [], 0, s[1:-1]
        while i < len(body):
            c = body[i]
            if c == "\\" and i + 1 < len(body):
                n = body[i + 1]
                if n == "u" and i + 5 < len(body) + 1:
                    try:
                        out.append(chr(int(body[i + 2:i + 6], 16)))
                        i += 6
                        continue
                    except ValueError:
                        pass
                out.append(_ESC.get(n, n))
                i += 2
                continue
            out.append(c)
            i += 1
        return "".join(out)
    return s


def _fold_quoted(lines: list) -> str:
    """Join the lines of a multi-line quoted scalar: line breaks become spaces, blank lines become newlines."""
    out = []
    for k, ln in enumerate(lines):
        t = ln.strip() if k else ln.rstrip()
        if not t and k:
            out.append("\n")
            continue
        if out and not out[-1].endswith("\n"):
            out.append(" ")
        out.append(t)
    return "".join(out)


class _Parser:
    def __init__(self, lines: list):
        self.lines = lines
        self.n = len(lines)
        self.virt = {}
        self.anchors = {}

    # ----------------------------------------------------------------- lines

    def _at(self, i: int):
        if i in self.virt:
            return self.virt[i]
        raw = self.lines[i]
        ind = len(raw) - len(raw.lstrip(" "))
        return ind, raw[ind:].rstrip()

    def _sig(self, i: int) -> int:
        while i < self.n:
            if i in self.virt:
                return i
            s = self.lines[i].strip()
            if s and not s.startswith("#") and not s.startswith("%"):
                return i
            i += 1
        return self.n

    @staticmethod
    def _is_item(s: str) -> bool:
        return s == "-" or s.startswith("- ") or s.startswith("-\t")

    @staticmethod
    def _key(s: str):
        """`key: rest` -> (key, rest), else None."""
        if not s or s[0] in "[{#|>" or _Parser._is_item(s) or s.startswith("? "):
            return None
        if s[0] in "\"'":
            j = _qend(s, 0)
            if j is None:
                return None
            rest = s[j + 1:].lstrip(" ")
            if rest.startswith(":") and (len(rest) == 1 or rest[1] in " \t"):
                return _unquote(s[:j + 1]), rest[1:].strip()
            return None
        if s[0] in "*&!":
            m0 = re.match(r"^[*&!]\S*\s+", s)
            if s[0] == "*" or not m0:
                return None
        depth, k = 0, 0
        while k < len(s):  # the key ends at the first ': ' outside ${{ }}
            if s.startswith("${{", k):
                depth += 1
                k += 3
                continue
            if depth and s.startswith("}}", k):
                depth -= 1
                k += 2
                continue
            c = s[k]
            if not depth:
                if c == "#" and k and s[k - 1] in " \t":
                    return None
                if c == ":" and (k + 1 == len(s) or s[k + 1] in " \t"):
                    key = s[:k].rstrip()
                    if not key:
                        return None
                    return key, s[k + 1:].strip()
            k += 1
        return None

    # ----------------------------------------------------------------- nodes

    def parse(self):
        i = self._sig(0)
        if i >= self.n:
            return None
        node, i = self.block(i, 0)
        i = self._sig(i)
        if i < self.n:
            raise YamlError(f"unexpected content: {self._at(i)[1][:50]}", i + 1)
        return node

    def block(self, i: int, min_ind: int):
        i = self._sig(i)
        if i >= self.n:
            return None, i
        ind, s = self._at(i)
        if ind < min_ind:
            return None, i
        if self._is_item(s):
            return self.seq(i, ind)
        props, rest = self._props(s)
        if self._key(rest) is not None:
            if props:
                self.virt[i] = (ind + len(s) - len(rest), rest)
                node, j = self.map(i, ind + len(s) - len(rest))
                return self._apply(props, node, i + 1), j
            return self.map(i, ind)
        return self.value(i, ind - 1, s, indentless=False)

    def map(self, i: int, ind: int):
        m = YMap(i + 1)
        merges = []
        while True:
            i = self._sig(i)
            if i >= self.n:
                break
            lind, s = self._at(i)
            if lind < ind or (lind == ind and self._is_item(s)):
                break
            if lind > ind:
                raise YamlError(f"unexpected indentation: {s[:50]}", i + 1)
            kv = self._key(s)
            if kv is None:
                raise YamlError(f"expected `key: value`, found: {s[:50]}", i + 1)
            key, rest = kv
            line = i + 1
            self.virt.pop(i, None)
            val, i = self.value(i, ind, rest, indentless=True)
            if key == "<<":
                merges.append(val)
                continue
            m[key] = val
            m.klines[key] = line
        for src in merges:
            for s_ in (src if isinstance(src, list) else [src]):
                if isinstance(s_, dict):
                    for k, v in s_.items():
                        if k not in m:
                            m[k] = v
                            m.klines[k] = getattr(s_, "klines", {}).get(k, m.line)
        return m, i

    def seq(self, i: int, ind: int):
        sq = YSeq(i + 1)
        while True:
            i = self._sig(i)
            if i >= self.n:
                break
            lind, s = self._at(i)
            if lind != ind or not self._is_item(s):
                if lind > ind:
                    raise YamlError(f"unexpected indentation: {s[:50]}", i + 1)
                break
            line = i + 1
            rest = s[1:].lstrip(" \t")
            col = lind + len(s) - len(rest)
            self.virt.pop(i, None)
            props, rest2 = self._props(rest)
            col2 = col + len(rest) - len(rest2)
            if rest2 and not rest2.startswith("#") and (self._key(rest2) is not None or self._is_item(rest2)):
                self.virt[i] = (col2, rest2)
                node, i = (self.map if self._key(rest2) is not None else self.seq)(i, col2)
                node = self._apply(props, node, line)
            else:
                node, i = self.value(i, ind, rest, indentless=False)
            sq.append(node)
            sq.ilines.append(line)
        return sq, i

    def _props(self, s: str):
        """Leading anchor and tag -> ([(kind, name)], rest)."""
        props = []
        while s[:1] in ("&", "!"):
            if s.startswith("!") and not s.startswith(("!!", "! ")) and re.match(r"^![A-Za-z]", s) is None:
                break
            tok, _, rest = s.partition(" ")
            props.append(("anchor" if tok.startswith("&") else "tag", tok[1:] if tok.startswith("&") else tok))
            s = rest.lstrip(" ")
        return props, s

    def _apply(self, props, node, line: int):
        for kind, name in props:
            if kind == "tag" and name not in ("!!str", "!!map", "!!seq", "!!int", "!!bool", "!!null", "!"):
                node = YTag(name, node, line)
            elif kind == "tag" and name == "!!str" and node is not None and not isinstance(node, (dict, list)):
                node = str(node)
        for kind, name in props:
            if kind == "anchor":
                self.anchors[name] = node
        return node

    def value(self, i: int, ind: int, rest: str, indentless: bool):
        """The value that follows a key (or a `- `) on line i, where the key sits at column `ind`."""
        line = i + 1
        props, rest = self._props(rest)
        if not rest or rest.startswith("#"):
            j = self._sig(i + 1)
            node, nxt = None, i + 1
            if j < self.n:
                jind, js = self._at(j)
                if jind > ind:
                    node, nxt = self.block(j, ind + 1)
                elif indentless and jind == ind and self._is_item(js):
                    node, nxt = self.seq(j, jind)
            return self._apply(props, node, line), nxt
        c = rest[0]
        if c in "|>":
            text, nxt = self._block_scalar(i, ind, _plain_cut(rest))
            return self._apply(props, text, line), nxt
        if c == "*":
            name = _plain_cut(rest)[1:]
            if name not in self.anchors:
                raise YamlError(f"unknown alias *{name}", line)
            return self.anchors[name], i + 1
        if c in "\"'":
            text, nxt = self._quoted(i, rest)
            return self._apply(props, text, line), nxt
        if c in "[{":
            text, nxt = self._flow_text(i, rest)
            try:
                node = _Flow(text, line, self.anchors).parse()
            except YamlError:
                raise
            except Exception as e:  # malformed flow text
                raise YamlError(f"cannot read flow collection: {e}", line)
            return self._apply(props, node, line), nxt
        parts = [_plain_cut(rest)]
        if re.search(r":(\s|$)", parts[0]):
            raise YamlError(f"a plain value cannot contain “: ” (quote it): {parts[0][:50]}", line)
        j = i + 1
        while j < self.n and not rest.rstrip().endswith(" #") and " #" not in rest:
            raw = self.lines[j]
            st = raw.strip()
            if not st:
                k = self._sig(j)
                if k < self.n and self._at(k)[0] > ind and not self.lines[k].strip().startswith("#"):
                    parts.append("\n")
                    j += 1
                    continue
                break
            if st.startswith("#") or (len(raw) - len(raw.lstrip(" "))) <= ind:
                break
            parts.append(_plain_cut(st))
            if re.search(r":(\s|$)", parts[-1]):
                raise YamlError(f"a plain value cannot contain “: ” (quote it): {parts[-1][:50]}", j + 1)
            if " #" in st:
                j += 1
                break
            j += 1
        text = parts[0]
        for p in parts[1:]:
            text = text + p if p == "\n" or text.endswith("\n") else text + " " + p
        return self._apply(props, _scalar(text) if len(parts) == 1 else text, line), j

    def _quoted(self, i: int, rest: str):
        end = _qend(rest, 0)
        if end is not None:
            tail = rest[end + 1:].strip()
            if tail and not tail.startswith("#"):
                raise YamlError(f"unexpected text after a quoted value: {tail[:40]}", i + 1)
            return _unquote(rest[:end + 1]), i + 1
        acc = [rest]
        j = i + 1
        while j < self.n:
            acc.append(self.lines[j])
            joined = "\n".join(acc)
            end = _qend(joined, 0)
            if end is not None:
                q = joined[0]
                body = _fold_quoted(joined[1:end].split("\n"))
                return _unquote(q + body + q) if q == '"' else body.replace("''", "'"), j + 1
            j += 1
        raise YamlError("unterminated quoted scalar", i + 1)

    def _flow_text(self, i: int, rest: str):
        """The whole flow collection starting at `rest`, joined across lines, comments removed."""
        acc, depth, j, text = [], 0, i, rest
        while True:
            q, k, expr = None, 0, 0
            buf = []
            while k < len(text):
                ch = text[k]
                if q:
                    buf.append(ch)
                    if ch == "\\" and q == '"' and k + 1 < len(text):
                        buf.append(text[k + 1])
                        k += 2
                        continue
                    if ch == q:
                        if q == "'" and text[k + 1:k + 2] == "'":
                            buf.append("'")
                            k += 2
                            continue
                        q = None
                    k += 1
                    continue
                if text.startswith("${{", k):
                    expr += 1
                    buf.append("${{")
                    k += 3
                    continue
                if expr and text.startswith("}}", k):
                    expr -= 1
                    buf.append("}}")
                    k += 2
                    continue
                if ch in "\"'" and (not buf or buf[-1] in " \t[{,:"):
                    q = ch
                elif ch == "#" and (not buf or buf[-1] in " \t"):
                    break
                elif ch in "[{" and not expr:
                    depth += 1
                elif ch in "]}" and not expr:
                    depth -= 1
                buf.append(ch)
                k += 1
            acc.append("".join(buf))
            if depth <= 0:
                return " ".join(acc), j + 1
            j += 1
            if j >= self.n:
                raise YamlError("unterminated flow collection", i + 1)
            text = self.lines[j].strip()

    def _block_scalar(self, i: int, ind: int, header: str):
        style = header[0]
        chomp = "+" if "+" in header else "-" if "-" in header else ""
        m = re.search(r"\d", header)
        explicit = int(m.group(0)) if m else None
        body, j = [], i + 1
        content_ind = None if explicit is None else max(ind, 0) + explicit
        while j < self.n:
            raw = self.lines[j]
            if raw.strip():
                rind = len(raw) - len(raw.lstrip(" "))
                if rind <= ind:
                    break
                if content_ind is None:
                    content_ind = rind
                if rind < content_ind:
                    break
            body.append(raw)
            j += 1
        trailing = 0
        while body and not body[-1].strip():  # trailing blank lines: kept only with `+` chomping
            body.pop()
            trailing += 1
        ci = content_ind or 0
        rows = [b[ci:] if len(b) >= ci else "" for b in body]
        if style == "|":
            text = "\n".join(rows)
        else:
            out, prev_more = [], False
            for r in rows:
                more = r.startswith((" ", "\t"))
                if not r:
                    out.append("\n")
                elif not out or out[-1].endswith("\n"):
                    out.append(r)
                elif more or prev_more:  # breaks next to more-indented lines are kept
                    out.append("\n" + r)
                else:
                    out.append(" " + r)
                prev_more = more if r else prev_more
            text = "".join(out)
        if chomp != "-" and text:
            text += "\n"
        if chomp == "+":
            text += "\n" * trailing
        return text, j


class _Flow:
    """Flow collections: [a, b], {k: v}, nested, quoted, aliases, ${{ }} kept whole."""

    def __init__(self, text: str, line: int, anchors: dict):
        self.t, self.p, self.line, self.anchors = text, 0, line, anchors

    def ws(self):
        while self.p < len(self.t) and self.t[self.p] in " \t\n":
            self.p += 1

    def parse(self):
        self.ws()
        node = self.node()
        return node

    def node(self, in_map_key=False):
        self.ws()
        t = self.t
        if self.p >= len(t):
            return None
        c = t[self.p]
        if c == "[":
            self.p += 1
            sq = YSeq(self.line)
            while True:
                self.ws()
                if self.p < len(t) and t[self.p] == "]":
                    self.p += 1
                    return sq
                item = self.node()
                self.ws()
                if self.p < len(t) and t[self.p] == ":" and (self.p + 1 >= len(t) or t[self.p + 1] in " ,]"):
                    self.p += 1  # [k: v] is a single-pair mapping
                    v = self.node()
                    m = YMap(self.line)
                    m[str(item)] = v
                    m.klines[str(item)] = self.line
                    item = m
                    self.ws()
                sq.append(item)
                sq.ilines.append(self.line)
                if self.p < len(t) and t[self.p] == ",":
                    self.p += 1
                    continue
                if self.p < len(t) and t[self.p] == "]":
                    self.p += 1
                    return sq
                raise ValueError(f"expected , or ] at {t[self.p:self.p + 20]!r}")
        if c == "{":
            self.p += 1
            m = YMap(self.line)
            while True:
                self.ws()
                if self.p < len(t) and t[self.p] == "}":
                    self.p += 1
                    return m
                k = self.node(in_map_key=True)
                self.ws()
                v = None
                if self.p < len(t) and t[self.p] == ":":
                    self.p += 1
                    v = self.node()
                    self.ws()
                key = "" if k is None else str(k) if not isinstance(k, bool) else str(k).lower()
                m[key] = v
                m.klines[key] = self.line
                if self.p < len(t) and t[self.p] == ",":
                    self.p += 1
                    continue
                if self.p < len(t) and t[self.p] == "}":
                    self.p += 1
                    return m
                raise ValueError(f"expected , or }} at {t[self.p:self.p + 20]!r}")
        if c in "\"'":
            end = _qend(t, self.p)
            if end is None:
                raise ValueError("unterminated quote")
            s = _unquote(t[self.p:end + 1])
            self.p = end + 1
            return s
        if c == "*":
            m = re.match(r"\*([^\s,\]\}]+)", t[self.p:])
            self.p += m.end()
            return self.anchors.get(m.group(1))
        start, expr = self.p, 0
        while self.p < len(t):
            if t.startswith("${{", self.p):
                expr += 1
                self.p += 3
                continue
            if expr and t.startswith("}}", self.p):
                expr -= 1
                self.p += 2
                continue
            ch = t[self.p]
            if not expr and ch in ",]}":
                break
            if not expr and ch == ":" and (self.p + 1 >= len(t) or t[self.p + 1] in " ,]}\t"):
                break
            self.p += 1
        return _scalar(t[start:self.p].strip())


def _documents(text: str) -> list:
    """Split a stream into documents: lists of lines that keep their position (blank lines elsewhere)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    docs, cur, seen = [], [""] * len(lines), False
    for k, ln in enumerate(lines):
        if ln.startswith("---") and (ln == "---" or ln[3:4] in (" ", "\t")):
            if seen:
                docs.append(cur)
                cur, seen = [""] * len(lines), False
            rest = ln[3:].strip()
            if rest and not rest.startswith("#"):
                cur[k] = rest
                seen = True
            continue
        if ln.rstrip() == "...":
            continue
        s = ln.strip()
        if s.startswith("%") and not seen:
            continue
        cur[k] = ln
        if s and not s.startswith("#"):
            seen = True
    if seen or not docs:
        docs.append(cur)
    return docs


def load_yaml(text: str):
    """Parse YAML (the subset CI files use) -> YMap / YSeq / scalars. Raises YamlError with the line.
    Several documents: the first, except a GitLab `spec:` header, which is followed by the pipeline."""
    docs = load_yaml_all(text)
    if len(docs) > 1 and isinstance(docs[0], dict) and set(docs[0]) <= {"spec"}:
        return docs[1]
    return docs[0] if docs else None


def load_yaml_all(text: str) -> list:
    return [_Parser(d).parse() for d in _documents(text)]


def plain(node):
    """A parsed node as plain dicts and lists (for comparisons and JSON)."""
    if isinstance(node, YTag):
        return {node.tag: plain(node.value)}
    if isinstance(node, dict):
        return {k: plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [plain(v) for v in node]
    return node


# =========================================================================== discovery

def _read(p: Path, limit: int = 2_000_000) -> str:
    try:
        return Path(p).read_text(errors="replace")[:limit]
    except OSError:
        return ""


def _rel(root: Path, p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(p)


def discover(root) -> dict:
    """{provider: [entry files relative to root]} for every pipeline in the repository."""
    root = Path(root)
    out = {}
    d = root / ".github" / "workflows"
    if d.is_dir():
        wf = sorted({f for f in list(d.glob("*.yml")) + list(d.glob("*.yaml")) if f.is_file()})
        if wf:
            out["github"] = [_rel(root, f) for f in wf]
    if (root / ".gitlab-ci.yml").is_file():
        out["gitlab"] = [".gitlab-ci.yml"]
    for name in ("bitbucket-pipelines.yml", "bitbucket-pipelines.yaml"):
        if (root / name).is_file():
            out["bitbucket"] = [name]
            break
    az = set()
    for pat in ("azure-pipelines.yml", "azure-pipelines.yaml", "*.azure-pipelines.yml", "*.azure-pipelines.yaml"):
        az |= {f for f in root.glob(pat) if f.is_file()}
    if (root / ".azure-pipelines").is_dir():
        az |= {f for f in (root / ".azure-pipelines").rglob("*.y*ml") if f.is_file()}
    if az:
        out["azure"] = sorted(_rel(root, f) for f in az)
    return out


# =========================================================================== the normalized model
#
# pipeline {provider, file, files, tree, triggers, jobs, notes, errors, ...provider extras}
# job      {provider, file, id, name, line, steps, images, timeout, cache, matrix, parallel, fail_fast, needs,
#           environment, manual, hidden_failure, retry, junit, coverage, on_pr, on_main, on_tag, ...}
# step     {line, name, run, uses, with, env, if}

def _job(provider: str, file: str, jid: str, line: int, name=None) -> dict:
    return {"provider": provider, "file": file, "id": str(jid), "name": str(name or jid), "line": line or 1,
            "steps": [], "runs_on": None, "runs_on_line": None, "images": [], "timeout": None, "cache": False,
            "matrix": False, "parallel": False, "fail_fast": None, "needs": [], "environment": None,
            "manual": False, "hidden_failure": [], "retry": [], "junit": False, "coverage": False,
            "on_pr": True, "on_main": True, "on_tag": True, "permissions": None, "reusable": False,
            "env": {}, "if": "", "self_hosted": False, "oidc": False, "line_of": {}}


def _step(line: int, run=None, uses=None, name=None, with_=None, env=None, cond=None, run_line=None) -> dict:
    return {"line": line, "run": run if isinstance(run, str) else None, "run_line": run_line or line,
            "uses": uses if isinstance(uses, str) else None, "name": name, "with": with_ if isinstance(with_, dict) else {},
            "env": env if isinstance(env, dict) else {}, "if": cond if isinstance(cond, str) else ""}


def _kl(m, key, default=None):
    return getattr(m, "klines", {}).get(key, default if default is not None else getattr(m, "line", 1))


def _as_list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, list) else [v]


def _text(v) -> str:
    """Every scalar of a node as text (for pattern searches)."""
    if isinstance(v, YTag):
        return _text(v.value)
    if isinstance(v, dict):
        return "\n".join(f"{k}: {_text(x)}" for k, x in v.items())
    if isinstance(v, list):
        return "\n".join(_text(x) for x in v)
    return "" if v is None else str(v)


# --------------------------------------------------------------------------- GitHub Actions

PRIVILEGED_EVENTS = ("pull_request_target", "workflow_run", "issue_comment", "issues", "discussion",
                     "discussion_comment")


def _gh_triggers(on) -> dict:
    t = {"pr": False, "push": False, "branches": [], "tags": [], "release": False, "schedule": False,
         "manual": False, "privileged": [], "paths": False, "call": False, "events": []}
    if isinstance(on, str):
        events = {on: None}
    elif isinstance(on, list):
        events = {str(e): None for e in on}
    elif isinstance(on, dict):
        events = dict(on)
    else:
        events = {}
    t["events"] = list(events)
    for ev, cfg in events.items():
        cfg = cfg if isinstance(cfg, dict) else {}
        if ev in ("pull_request", "merge_group"):
            t["pr"] = True
            t["paths"] |= bool(cfg.get("paths") or cfg.get("paths-ignore"))
        elif ev == "push":
            t["push"] = True
            br = cfg.get("branches") or cfg.get("branches-ignore")
            tg = cfg.get("tags") or cfg.get("tags-ignore")
            if br is None and tg is None:
                t["branches"], t["tags"] = ["**"], ["**"]
            else:
                t["branches"] = [str(x) for x in _as_list(cfg.get("branches"))] or (["**"] if cfg.get("branches-ignore") else [])
                t["tags"] = [str(x) for x in _as_list(cfg.get("tags"))] or (["**"] if cfg.get("tags-ignore") else [])
            t["paths"] |= bool(cfg.get("paths") or cfg.get("paths-ignore"))
        elif ev == "release":
            t["release"] = True
        elif ev == "schedule":
            t["schedule"] = True
        elif ev == "workflow_dispatch":
            t["manual"] = True
        elif ev == "workflow_call":
            t["call"] = True
        if ev in PRIVILEGED_EVENTS:
            t["privileged"].append(ev)
    return t


def _gh_job_scope(job: dict, trig: dict):
    cond = job["if"] or ""
    tag_only = bool(re.search(r"refs/tags/|ref_type\s*==\s*'tag'|ref_type\s*==\s*\"tag\"", cond)) and "!" not in \
        cond.split("refs/tags")[0][-15:]
    push_only = bool(re.search(r"event_name\s*==\s*'(push|release|workflow_dispatch|schedule)'", cond))
    pr_only = bool(re.search(r"event_name\s*==\s*'pull_request'", cond)) and "!=" not in cond
    main_only = bool(re.search(r"github\.ref\s*==\s*'refs/heads/", cond))
    job["on_pr"] = trig["pr"] and not (tag_only or push_only or main_only)
    job["on_tag"] = (bool(trig["tags"]) or trig["release"]) and not (pr_only or main_only)
    job["on_main"] = (bool(trig["branches"]) or trig["call"]) and not (tag_only or pr_only)


def build_github(file: str, text: str, tree) -> dict:
    pl = {"provider": "github", "file": file, "files": [file], "text": text, "tree": tree, "jobs": [], "notes": []}
    if not isinstance(tree, dict):
        pl["triggers"] = _gh_triggers(None)
        return pl
    on = tree.get("on", tree.get(True))
    trig = _gh_triggers(on)
    pl["triggers"] = trig
    pl["name"] = tree.get("name") or file
    pl["permissions"] = tree.get("permissions", "__missing__")
    pl["permissions_line"] = _kl(tree, "permissions", 1)
    pl["concurrency"] = tree.get("concurrency")
    pl["env"] = tree.get("env") if isinstance(tree.get("env"), dict) else {}
    jobs = tree.get("jobs") if isinstance(tree.get("jobs"), dict) else {}
    pl["jobs_line"] = _kl(tree, "jobs", 1)
    for jid, j in jobs.items():
        if not isinstance(j, dict):
            continue
        job = _job("github", file, jid, _kl(jobs, jid), j.get("name"))
        job["map"] = j
        job["if"] = str(j.get("if") or "")
        ro = j.get("runs-on")
        job["runs_on"] = ro if not isinstance(ro, dict) else (ro.get("labels") or ro.get("group"))
        job["runs_on_line"] = _kl(j, "runs-on", job["line"])
        labels = [str(x) for x in _as_list(job["runs_on"])]
        job["self_hosted"] = any(x == "self-hosted" for x in labels) or (isinstance(ro, dict) and bool(ro.get("group")))
        c = j.get("container")
        if c:
            img = c.get("image") if isinstance(c, dict) else c
            if img:
                job["images"].append((str(img), _kl(j, "container", job["line"])))
        for sname, sv in (j.get("services") or {}).items() if isinstance(j.get("services"), dict) else []:
            img = sv.get("image") if isinstance(sv, dict) else sv
            if img:
                job["images"].append((str(img), _kl(j["services"], sname, job["line"])))
        job["timeout"] = j.get("timeout-minutes")
        st = j.get("strategy") if isinstance(j.get("strategy"), dict) else {}
        job["matrix"] = bool(st.get("matrix"))
        job["fail_fast"] = st.get("fail-fast")
        job["fail_fast_line"] = _kl(j, "strategy", job["line"])
        job["needs"] = [str(x) for x in _as_list(j.get("needs"))]
        env_ = j.get("environment")
        job["environment"] = (env_.get("name") if isinstance(env_, dict) else env_) or None
        job["permissions"] = j.get("permissions", "__missing__")
        job["permissions_line"] = _kl(j, "permissions", job["line"])
        job["reusable"] = bool(j.get("uses"))
        job["env"] = dict(pl["env"], **(j.get("env") if isinstance(j.get("env"), dict) else {}))
        if j.get("continue-on-error") is True:
            job["hidden_failure"].append(("continue-on-error: true", _kl(j, "continue-on-error")))
        if job["reusable"]:
            job["steps"].append(_step(_kl(j, "uses"), uses=str(j.get("uses")), with_=j.get("with")))
        steps = j.get("steps") if isinstance(j.get("steps"), list) else []
        for k, s_ in enumerate(steps):
            if not isinstance(s_, dict):
                continue
            ln = steps.ilines[k] if isinstance(steps, YSeq) and k < len(steps.ilines) else job["line"]
            stp = _step(ln, run=s_.get("run"), uses=s_.get("uses"), name=s_.get("name"), with_=s_.get("with"),
                        env=s_.get("env"), cond=s_.get("if"), run_line=_kl(s_, "run", ln))
            stp["uses_line"] = _kl(s_, "uses", ln)
            stp["map"] = s_
            if s_.get("continue-on-error") is True:
                job["hidden_failure"].append((f"continue-on-error: true on step “{s_.get('name') or s_.get('uses') or 'run'}”",
                                              _kl(s_, "continue-on-error", ln), f"{s_.get('run') or ''} {s_.get('uses') or ''}"))
            job["steps"].append(stp)
        _gh_job_scope(job, trig)
        pl["jobs"].append(job)
    return pl


# --------------------------------------------------------------------------- GitLab CI

GL_RESERVED = {"stages", "variables", "default", "include", "workflow", "image", "services", "cache",
               "before_script", "after_script", "types", "spec", "pages:deploy"} - {"pages:deploy"}
GL_DEFAULT_KEYS = ("image", "services", "before_script", "after_script", "cache", "interruptible", "retry",
                   "timeout", "tags", "artifacts", "hooks", "id_tokens")


def _deep_merge(base, over):
    """GitLab's merge for extends/include: mappings merge, everything else is replaced."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = YMap(getattr(over, "line", 0) or getattr(base, "line", 0))
        for k, v in base.items():
            out[k] = v
            out.klines[k] = _kl(base, k)
        for k, v in over.items():
            out[k] = _deep_merge(base[k], v) if k in base else v
            out.klines[k] = _kl(over, k)
        return out
    return over


def _gl_includes(root: Path, file: str, tree, seen: set, depth=0):
    """-> ([(file, tree)], [remote include names]) for `include:`, local files resolved recursively."""
    local, remote = [], []
    if not isinstance(tree, dict) or depth > 8:
        return local, remote
    for inc in _as_list(tree.get("include")):
        paths = []
        if isinstance(inc, str):
            (remote if re.match(r"^https?://", inc) else paths).append(inc)
        elif isinstance(inc, dict):
            if inc.get("local"):
                paths.append(str(inc["local"]))
            for k in ("template", "remote", "component"):
                if inc.get(k):
                    remote.append(f"{k}:{inc[k]}")
            if inc.get("project"):
                remote += [f"project:{inc['project']}/{f}" for f in _as_list(inc.get("file"))]
        for pth in paths:
            pat = pth.lstrip("/")
            matches = sorted(root.glob(pat)) if any(c in pat for c in "*?[") else [root / pat]
            for m in matches:
                rel = _rel(root, m)
                if rel in seen or not m.is_file():
                    if not m.exists():
                        remote.append(f"missing:{pth}")
                    continue
                seen.add(rel)
                try:
                    sub = load_yaml(_read(m))
                except YamlError as e:
                    local.append((rel, None, e))
                    continue
                l2, r2 = _gl_includes(root, rel, sub, seen, depth + 1)
                local += l2 + [(rel, sub, None)]
                remote += r2
    return local, remote


def _gl_resolve_refs(node, top, depth=0):
    """Replace !reference [.job, key, ...] with what it points to; lists of references are flattened."""
    if depth > 12:
        return node
    if isinstance(node, YTag) and node.tag == "!reference":
        cur = top
        for part in _as_list(node.value):
            cur = cur.get(str(part)) if isinstance(cur, dict) else None
        return _gl_resolve_refs(cur, top, depth + 1)
    if isinstance(node, dict):
        out = YMap(getattr(node, "line", 0))
        for k, v in node.items():
            out[k] = _gl_resolve_refs(v, top, depth + 1)
            out.klines[k] = _kl(node, k)
        return out
    if isinstance(node, list):
        out = YSeq(getattr(node, "line", 0))
        ilines = getattr(node, "ilines", [])
        for k, v in enumerate(node):
            r = _gl_resolve_refs(v, top, depth + 1)
            ln = ilines[k] if k < len(ilines) else getattr(node, "line", 0)
            if isinstance(v, YTag) and isinstance(r, list):  # a referenced list is spliced in
                out.extend(r)
                out.ilines += list(getattr(r, "ilines", [])) or [ln] * len(r)
            else:
                out.append(r)
                out.ilines.append(ln)
        return out
    return node


def _gl_extends(name: str, top: dict, depth=0):
    j = top.get(name)
    if not isinstance(j, dict) or depth > 10:
        return j
    parents = _as_list(j.get("extends"))
    if not parents:
        return j
    merged = YMap(j.line)
    for p_ in parents:
        base = _gl_extends(str(p_), top, depth + 1)
        if isinstance(base, dict):
            merged = _deep_merge(merged, base)
    return _deep_merge(merged, j)


def _gl_rule_kinds(rules, only=None, except_=None) -> set:
    """Which pipelines a job (or the workflow) runs in: {"mr", "main", "tag", "branch"}."""
    if only is not None or except_ is not None:
        refs = only.get("refs") if isinstance(only, dict) else only
        refs = [str(r) for r in _as_list(refs)]
        kinds = set()
        for r in refs or ["branches", "tags"]:
            if r == "merge_requests":
                kinds.add("mr")
            elif r == "tags":
                kinds.add("tag")
            elif r in ("branches", "pushes", "web", "api", "triggers", "schedules"):
                kinds |= {"branch", "main"}
            elif r in ("main", "master") or "default" in r.lower():
                kinds.add("main")
            else:
                kinds |= {"branch", "main"}
        ex = [str(r) for r in _as_list(except_.get("refs") if isinstance(except_, dict) else except_)]
        if "tags" in ex:
            kinds.discard("tag")
        return kinds
    if rules is None:
        return {"branch", "main", "tag"}
    kinds = set()
    for r in _as_list(rules):
        if not isinstance(r, dict):
            continue
        if str(r.get("when") or "") == "never":
            continue
        cond = str(r.get("if") or "")
        if not cond:
            kinds |= {"branch", "main", "tag", "mr"}
            continue
        k = set()
        if re.search(r"merge_request_event|CI_MERGE_REQUEST_|CI_OPEN_MERGE_REQUESTS", cond) and \
                not re.search(r"(CI_PIPELINE_SOURCE\s*!=\s*[\"']merge_request_event|\$CI_OPEN_MERGE_REQUESTS\s*&&)", cond):
            k.add("mr")
        if re.search(r"(?<!!)\$CI_COMMIT_TAG(?!\s*==\s*null)", cond) and not re.search(r"CI_COMMIT_TAG\s*==\s*null", cond):
            k.add("tag")
        if re.search(r"CI_COMMIT_BRANCH\s*==\s*(\$CI_DEFAULT_BRANCH|[\"'](main|master)[\"'])|"
                     r"CI_COMMIT_REF_NAME\s*==\s*(\$CI_DEFAULT_BRANCH|[\"'](main|master)[\"'])", cond):
            k.add("main")
        elif re.search(r"\$CI_COMMIT_BRANCH(?!\s*==)|CI_PIPELINE_SOURCE\s*==\s*[\"']push", cond):
            k |= {"branch", "main"}
        if not k:
            k = {"branch", "main", "tag", "mr"}
        kinds |= k
    return kinds


def build_gitlab(root: Path, file: str, text: str, tree) -> dict:
    pl = {"provider": "gitlab", "file": file, "files": [file], "text": text, "tree": tree, "jobs": [], "notes": [],
          "remote_includes": [], "errors": []}
    if not isinstance(tree, dict):
        pl["triggers"] = {"pr": True, "tags": [], "branches": ["**"], "paths": False}
        return pl
    includes, remote = _gl_includes(root, file, tree, {file})
    pl["remote_includes"] = remote
    top, origin = YMap(tree.line), {}
    for f, sub, err in includes:
        pl["files"].append(f)
        if err is not None:
            pl["errors"].append((f, err))
            continue
        if isinstance(sub, dict):
            for k, v in sub.items():
                top[k] = _deep_merge(top[k], v) if k in top else v
                top.klines[k] = _kl(sub, k)
                origin[k] = f
    for k, v in tree.items():
        top[k] = _deep_merge(top[k], v) if k in top and k not in ("include",) else v
        top.klines[k] = _kl(tree, k)
        origin[k] = file
    pl["top"] = top
    default = top.get("default") if isinstance(top.get("default"), dict) else YMap()
    for k in ("image", "services", "before_script", "after_script", "cache"):
        if k in top and k not in default:
            default[k] = top[k]
            default.klines[k] = _kl(top, k)
    pl["default"] = default
    wf = top.get("workflow") if isinstance(top.get("workflow"), dict) else {}
    wf_kinds = _gl_rule_kinds(wf.get("rules")) if wf.get("rules") is not None else {"branch", "main", "tag", "mr"}
    if wf.get("rules") is not None and "mr" not in wf_kinds and "branch" in wf_kinds:
        wf_kinds.add("mr")  # branch pipelines run for merge request branches too
    pl["workflow_kinds"] = wf_kinds
    any_changes = False
    tags_rules = []
    for name in list(top):
        if name in GL_RESERVED or name.startswith(".") or not isinstance(top.get(name), dict):
            continue
        raw = top[name]
        if not any(k in raw for k in ("script", "trigger", "run", "extends", "stage")):
            continue
        j = _gl_resolve_refs(_gl_extends(name, top), top)
        inherit = j.get("inherit") if isinstance(j.get("inherit"), dict) else {}
        use_default = inherit.get("default", True)
        eff = YMap(j.line)
        if use_default is not False:
            for k in GL_DEFAULT_KEYS:
                if k in default and (use_default is True or k in _as_list(use_default)):
                    eff[k] = _gl_resolve_refs(default[k], top)
                    eff.klines[k] = _kl(default, k)
        for k, v in j.items():
            eff[k] = v
            eff.klines[k] = _kl(j, k)
        f = origin.get(name, file)
        job = _job("gitlab", f, name, _kl(top, name))
        job["map"], job["own"] = eff, raw
        img = eff.get("image")
        img = img.get("name") if isinstance(img, dict) else img
        if img:
            job["images"].append((str(img), _kl(raw, "image", job["line"]) if "image" in raw else
                                  _kl(default, "image", job["line"])))
        for sv in _as_list(eff.get("services")):
            n = sv.get("name") if isinstance(sv, dict) else sv
            if n:
                job["images"].append((str(n), _kl(eff, "services", job["line"])))
        for key in ("before_script", "script", "after_script"):
            for txt, ln in _script_items(eff.get(key), _kl(eff, key, job["line"])):
                job["steps"].append(_step(ln, run=txt, name=key))
        if eff.get("trigger"):
            job["steps"].append(_step(job["line"], name="trigger", uses=f"trigger:{_text(eff.get('trigger'))[:80]}"))
        job["timeout"] = eff.get("timeout")
        job["cache"] = bool(eff.get("cache"))
        arts = eff.get("artifacts") if isinstance(eff.get("artifacts"), dict) else {}
        reports = arts.get("reports") if isinstance(arts.get("reports"), dict) else {}
        job["junit"] = bool(reports.get("junit"))
        job["coverage"] = bool(eff.get("coverage") or reports.get("coverage_report"))
        par = eff.get("parallel")
        job["parallel"] = bool(par) and (not isinstance(par, int) or par > 1)
        job["matrix"] = isinstance(par, dict) and bool(par.get("matrix"))
        job["interruptible"] = eff.get("interruptible")
        env_ = eff.get("environment")
        job["environment"] = (env_.get("name") if isinstance(env_, dict) else env_) or None
        job["manual"] = str(eff.get("when") or "") == "manual" or any(
            isinstance(r, dict) and str(r.get("when") or "") == "manual" for r in _as_list(eff.get("rules")))
        af = eff.get("allow_failure")
        if af is True:
            job["hidden_failure"].append(("allow_failure: true", _kl(eff, "allow_failure", job["line"])))
        rt = eff.get("retry")
        if rt is not None and rt != 0:
            when = rt.get("when") if isinstance(rt, dict) else None
            job["retry"].append((rt.get("max") if isinstance(rt, dict) else rt, when, _kl(eff, "retry", job["line"])))
        job["needs"] = [str(n.get("job") if isinstance(n, dict) else n) for n in _as_list(eff.get("needs"))]
        job["stage"] = str(eff.get("stage") or "test")
        job["id_tokens"] = bool(eff.get("id_tokens"))
        job["release_kw"] = bool(eff.get("release"))
        job["runner_tags"] = [str(t) for t in _as_list(eff.get("tags"))]
        kinds = _gl_rule_kinds(eff.get("rules"), eff.get("only"), eff.get("except")) & wf_kinds
        if eff.get("rules") is None and eff.get("only") is None and "mr" in wf_kinds and \
                wf.get("rules") is not None and "branch" not in wf_kinds:
            kinds.add("mr")
        job["on_pr"] = bool(kinds & {"mr", "branch"})
        job["on_main"] = "main" in kinds
        job["on_tag"] = "tag" in kinds
        if any(isinstance(r, dict) and r.get("changes") for r in _as_list(eff.get("rules"))) or \
                isinstance(eff.get("only"), dict) and eff["only"].get("changes"):
            any_changes = True
        if job["on_tag"]:
            tags_rules += [str(r.get("if")) for r in _as_list(eff.get("rules")) if isinstance(r, dict) and
                           "CI_COMMIT_TAG" in str(r.get("if") or "")]
        pl["jobs"].append(job)
    pl["triggers"] = {"pr": "mr" in wf_kinds or "branch" in wf_kinds, "branches": ["**"],
                      "tags": tags_rules or (["**"] if any(j["on_tag"] for j in pl["jobs"]) else []),
                      "paths": any_changes}
    return pl


def _script_items(node, line: int) -> list:
    """GitLab script / Bitbucket script -> [(text, line)], nested lists flattened."""
    out = []
    if node is None:
        return out
    if isinstance(node, str):
        return [(node, line)]
    if isinstance(node, list):
        ilines = getattr(node, "ilines", [])
        for k, v in enumerate(node):
            ln = ilines[k] if k < len(ilines) else line
            if isinstance(v, list):
                out += _script_items(v, ln)
            elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
                out.append((str(v), ln))
    return out


# --------------------------------------------------------------------------- Bitbucket Pipelines

def build_bitbucket(file: str, text: str, tree) -> dict:
    pl = {"provider": "bitbucket", "file": file, "files": [file], "text": text, "tree": tree, "jobs": [], "notes": []}
    trig = {"pr": False, "branches": [], "tags": [], "paths": False, "default": False}
    pl["triggers"] = trig
    if not isinstance(tree, dict):
        return pl
    opts = tree.get("options") if isinstance(tree.get("options"), dict) else {}
    pl["max_time"] = opts.get("max-time")
    top_image = tree.get("image")
    top_image = top_image.get("name") if isinstance(top_image, dict) else top_image
    pls = tree.get("pipelines") if isinstance(tree.get("pipelines"), dict) else {}
    counter = {"n": 0}

    def add_step(st, kind, pattern, sline, extra=None):
        if not isinstance(st, dict):
            return
        counter["n"] += 1
        name = st.get("name") or f"step {counter['n']}"
        sline = getattr(st, "line", 0) or sline  # the step's definition: an anchor reused by several pipelines
        job = _job("bitbucket", file, f"{kind}:{pattern}:{name}" if pattern else f"{kind}:{name}", sline, name)
        job["map"] = st
        img = st.get("image")
        img = img.get("name") if isinstance(img, dict) else img
        if img:
            job["images"].append((str(img), _kl(st, "image", sline)))
        elif top_image:
            job["images"].append((str(top_image), _kl(tree, "image", 1)))
        sc = st.get("script")
        ilines = getattr(sc, "ilines", [])
        for k, item in enumerate(_as_list(sc)):
            ln = ilines[k] if k < len(ilines) else _kl(st, "script", sline)
            if isinstance(item, dict) and item.get("pipe"):
                job["steps"].append(_step(ln, uses=f"pipe:{item['pipe']}", with_=item.get("variables")))
            elif isinstance(item, (str, int)) and not isinstance(item, bool):
                job["steps"].append(_step(ln, run=str(item)))
        for txt, ln in _script_items(st.get("after-script"), _kl(st, "after-script", sline)):
            job["steps"].append(_step(ln, run=txt, name="after-script"))
        job["cache"] = bool(st.get("caches"))
        job["timeout"] = st.get("max-time") or pl["max_time"]
        job["environment"] = st.get("deployment") or (extra or {}).get("deployment")
        job["manual"] = str(st.get("trigger") or (extra or {}).get("trigger") or "") == "manual"
        job["oidc"] = bool(st.get("oidc"))
        job["self_hosted"] = "self.hosted" in [str(x) for x in _as_list(st.get("runs-on"))]
        job["parallel"] = bool((extra or {}).get("parallel"))
        job["fail_fast"] = (extra or {}).get("fail_fast")
        cond = st.get("condition") if isinstance(st.get("condition"), dict) else {}
        if cond.get("changesets"):
            trig["paths"] = True
        job["on_pr"] = kind in ("default", "pull-requests")
        job["on_main"] = kind in ("default",) or (kind == "branches" and _bb_glob(pattern, "main"))
        job["on_tag"] = kind == "tags"
        job["kind"] = kind
        pl["jobs"].append(job)

    def walk(items, kind, pattern, extra=None):
        ilines = getattr(items, "ilines", [])
        for k, it in enumerate(_as_list(items)):
            ln = ilines[k] if k < len(ilines) else 1
            if not isinstance(it, dict):
                continue
            if "step" in it:
                add_step(it["step"], kind, pattern, ln, extra)
            elif "parallel" in it:
                par = it["parallel"]
                steps = par.get("steps") if isinstance(par, dict) else par
                walk(steps, kind, pattern, dict(extra or {}, parallel=True,
                                                fail_fast=par.get("fail-fast") if isinstance(par, dict) else None))
            elif "stage" in it and isinstance(it["stage"], dict):
                sg = it["stage"]
                walk(sg.get("steps"), kind, pattern, dict(extra or {}, deployment=sg.get("deployment"),
                                                          trigger=sg.get("trigger")))

    for kind, val in pls.items():
        if kind == "default":
            trig["default"] = trig["pr"] = True
            trig["branches"].append("**")
            walk(val, "default", "")
        elif kind in ("branches", "tags", "bookmarks", "pull-requests", "custom") and isinstance(val, dict):
            for pattern, items in val.items():
                if kind == "pull-requests":
                    trig["pr"] = True
                elif kind == "branches":
                    trig["branches"].append(str(pattern))
                elif kind == "tags":
                    trig["tags"].append(str(pattern))
                walk(items, kind, str(pattern))
    return pl


def _bb_glob(pattern: str, value: str) -> bool:
    """Bitbucket and Azure branch/tag globs: * (not /), ** (anything), {a,b}."""
    rx, k = "", 0
    while k < len(pattern):
        c = pattern[k]
        if pattern.startswith("**", k):
            rx += ".*"
            k += 2
            continue
        if c == "*":
            rx += "[^/]*"
        elif c == "?":
            rx += "."
        elif c == "{":
            end = pattern.find("}", k)
            if end > k:
                rx += "(" + "|".join(re.escape(x) for x in pattern[k + 1:end].split(",")) + ")"
                k = end + 1
                continue
            rx += re.escape(c)
        else:
            rx += re.escape(c)
        k += 1
    return re.fullmatch(rx, value) is not None


def gh_glob(pattern: str, value: str) -> bool:
    """GitHub Actions filter patterns: * (not /), ** (anything), ? and + after a character, [a-z] classes."""
    rx, k = "", 0
    while k < len(pattern):
        c = pattern[k]
        if pattern.startswith("**", k):
            rx += ".*"
            k += 2
            continue
        if c == "*":
            rx += "[^/]*"
        elif c in "?+":
            rx += c
        elif c == "[":
            end = pattern.find("]", k)
            if end > k:
                rx += pattern[k:end + 1]
                k = end + 1
                continue
            rx += re.escape(c)
        else:
            rx += re.escape(c)
        k += 1
    try:
        return re.fullmatch(rx, value) is not None
    except re.error:
        return fnmatch.fnmatch(value, pattern)


# --------------------------------------------------------------------------- Azure Pipelines

def _az_expand(node):
    """Splice template-expression keys (`${{ if … }}:`, `${{ each … }}:`) into their parent."""
    if isinstance(node, dict):
        out = YMap(getattr(node, "line", 0))
        for k, v in node.items():
            if isinstance(k, str) and k.startswith("${{") and isinstance(v, dict):
                for k2, v2 in _az_expand(v).items():
                    out[k2] = v2
                    out.klines[k2] = _kl(v, k2)
            else:
                out[k] = _az_expand(v)
                out.klines[k] = _kl(node, k)
        return out
    if isinstance(node, list):
        out = YSeq(getattr(node, "line", 0))
        ilines = getattr(node, "ilines", [])
        for k, v in enumerate(node):
            ln = ilines[k] if k < len(ilines) else getattr(node, "line", 0)
            if isinstance(v, dict) and len(v) == 1 and str(next(iter(v))).startswith("${{") and \
                    isinstance(next(iter(v.values())), list):
                inner = _az_expand(next(iter(v.values())))
                out.extend(inner)
                out.ilines += list(getattr(inner, "ilines", [])) or [ln] * len(inner)
            else:
                out.append(_az_expand(v))
                out.ilines.append(ln)
        return out
    return node


def _az_template(root: Path, base_file: str, ref: str, seen: set):
    """A local template -> (relative file, tree) or (None, None) for templates in other repositories."""
    ref = str(ref)
    if "@" in ref or "${{" in ref or "$(" in ref:
        return None, None
    p = (root / ref.lstrip("/")) if ref.startswith("/") else (root / Path(base_file).parent / ref)
    rel = _rel(root, p)
    if rel in seen or not p.is_file():
        return None, None
    try:
        return rel, _az_expand(load_yaml(_read(p)))
    except YamlError:
        return None, None


def build_azure(root: Path, file: str, text: str, tree) -> dict:
    pl = {"provider": "azure", "file": file, "files": [file], "text": text, "tree": tree, "jobs": [], "notes": []}
    tree = _az_expand(tree) if isinstance(tree, dict) else tree
    trig = {"pr": True, "branches": ["**"], "tags": [], "paths": False, "batch": False, "auto_cancel": True}
    pl["triggers"] = trig
    if not isinstance(tree, dict):
        return pl
    t = tree.get("trigger", "__absent__")
    if t == "none" or t is None and "trigger" in tree:
        trig["branches"] = []
    elif isinstance(t, list):
        trig["branches"] = [str(x) for x in t]
    elif isinstance(t, dict):
        br = t.get("branches")
        trig["branches"] = [str(x) for x in _as_list(br.get("include") if isinstance(br, dict) else br)] or ["**"]
        tg = t.get("tags")
        trig["tags"] = [str(x) for x in _as_list(tg.get("include") if isinstance(tg, dict) else tg)]
        trig["batch"] = t.get("batch") is True
        trig["paths"] |= bool(t.get("paths"))
    pr = tree.get("pr", "__absent__")
    if pr == "none" or pr is None and "pr" in tree:
        trig["pr"] = False
    elif isinstance(pr, dict):
        trig["auto_cancel"] = pr.get("autoCancel") is not False
        pl["auto_cancel_line"] = _kl(pr, "autoCancel", _kl(tree, "pr"))
        trig["paths"] |= bool(pr.get("paths"))
    pl["trigger_line"] = _kl(tree, "trigger", 1)
    seen = {file}
    ext = tree.get("extends")
    if isinstance(ext, dict) and ext.get("template"):
        f2, t2 = _az_template(root, file, ext["template"], seen)
        params = ext.get("parameters") if isinstance(ext.get("parameters"), dict) else {}
        if t2 is None and any(k in params for k in ("stages", "jobs", "steps")):
            pl["notes"].append(f"extends {ext['template']}: audited the stages and jobs passed to it")
            for k in ("stages", "jobs", "steps", "pool"):
                if k in params and k not in tree:
                    tree[k] = params[k]
                    tree.klines[k] = _kl(params, k)
        elif t2 is None:
            pl["notes"].append(f"extends a template from another repository ({ext['template']}): audited what this "
                               f"file shows")
        else:
            seen.add(f2)
            pl["files"].append(f2)
            tree = _deep_merge(t2, YMap()) if isinstance(t2, dict) else tree
    top_pool = tree.get("pool")

    def steps_of(items, base_file, depth=0):
        out = []
        ilines = getattr(items, "ilines", [])
        for k, s_ in enumerate(_as_list(items)):
            ln = ilines[k] if k < len(ilines) else 1
            if not isinstance(s_, dict):
                continue
            if "template" in s_ and depth < 6:
                f2, t2 = _az_template(root, base_file, s_["template"], seen)
                if t2 is not None:
                    pl["files"].append(f2)
                    out += [(f2, x) for x in steps_of_tree(t2, f2, depth + 1)]
                continue
            run = next((s_.get(k2) for k2 in ("script", "bash", "pwsh", "powershell") if k2 in s_), None)
            task = s_.get("task")
            stp = _step(ln, run=run, uses=f"task:{task}" if task else None, name=s_.get("displayName"),
                        with_=s_.get("inputs"), env=s_.get("env"), cond=s_.get("condition"))
            stp["map"] = s_
            out.append((base_file, stp))
        return out

    def steps_of_tree(t2, f2, depth):
        if isinstance(t2, dict):
            if t2.get("steps"):
                return [s for _, s in steps_of(t2["steps"], f2, depth)]
            jobs_ = []
            for j in _as_list(t2.get("jobs")):
                if isinstance(j, dict):
                    jobs_ += [s for _, s in steps_of(j.get("steps"), f2, depth)]
            return jobs_
        return []

    def add_job(j, base_file, line, stage=None, stage_cond=""):
        if not isinstance(j, dict):
            return
        if "template" in j and not ("job" in j or "deployment" in j):
            f2, t2 = _az_template(root, base_file, j["template"], seen)
            if t2 is not None:
                pl["files"].append(f2)
                jl = t2.get("jobs") if isinstance(t2, dict) else None
                jl_lines = getattr(jl, "ilines", [])
                for k, j2 in enumerate(_as_list(jl)):
                    add_job(j2, f2, jl_lines[k] if k < len(jl_lines) else 1, stage)
            return
        jid = j.get("job") or j.get("deployment") or "job"
        job = _job("azure", base_file, jid, line, j.get("displayName"))
        job["map"] = j
        job["deployment"] = "deployment" in j
        pool = j.get("pool", top_pool)
        vm = pool.get("vmImage") if isinstance(pool, dict) else None
        job["runs_on"] = vm or (pool.get("name") if isinstance(pool, dict) else pool)
        job["runs_on_line"] = _kl(j, "pool", line)
        job["self_hosted"] = bool(isinstance(pool, dict) and pool.get("name") and not vm) or isinstance(pool, str)
        c = j.get("container")
        if c:
            img = c.get("image") if isinstance(c, dict) else c
            job["images"].append((str(img), _kl(j, "container", line)))
        job["timeout"] = j.get("timeoutInMinutes")
        st = j.get("strategy") if isinstance(j.get("strategy"), dict) else {}
        job["matrix"] = bool(st.get("matrix"))
        job["parallel"] = bool(st.get("parallel")) or job["matrix"]
        if j.get("continueOnError") is True:
            job["hidden_failure"].append(("continueOnError: true", _kl(j, "continueOnError", line)))
        env_ = j.get("environment")
        job["environment"] = (env_.get("name") if isinstance(env_, dict) else env_) or None
        steps = j.get("steps")
        if job["deployment"] and not steps and isinstance(st, dict):
            for phase in st.values():
                if isinstance(phase, dict):
                    for hook in phase.values():
                        if isinstance(hook, dict) and hook.get("steps"):
                            steps = (steps or []) + list(hook["steps"])
        for f2, stp in steps_of(steps, base_file):
            m_ = stp.get("map") or {}
            if m_.get("continueOnError") is True:
                job["hidden_failure"].append((f"continueOnError: true on step “{stp['name'] or stp['uses'] or 'script'}”",
                                              _kl(m_, "continueOnError", stp["line"]), f"{stp['run'] or ''} {stp['uses'] or ''}"))
            if m_.get("retryCountOnTaskFailure"):
                job["retry"].append((m_.get("retryCountOnTaskFailure"), None, _kl(m_, "retryCountOnTaskFailure",
                                                                                   stp["line"])))
            if f2 == base_file:
                job["steps"].append(stp)
            else:
                stp2 = dict(stp)
                stp2["line"] = line  # findings point at the job that uses the template
                stp2["run_line"] = line
                job["steps"].append(stp2)
        job["stage"] = stage
        cond = f"{stage_cond} {j.get('condition') or ''}"
        tag_only = bool(re.search(r"startsWith\(variables\[['\"]Build\.SourceBranch['\"]\],\s*['\"]refs/tags/", cond, re.I))
        not_pr = bool(re.search(r"(ne\(|not\(eq\()variables\[['\"]Build\.Reason['\"]\],\s*['\"]PullRequest['\"]|"
                                r"eq\(variables\[['\"]Build\.(SourceBranch|SourceBranchName)['\"]\],\s*['\"](refs/heads/)?[\w./-]+['\"]\)|"
                                r"eq\(variables\[['\"]Build\.Reason['\"]\],\s*['\"](IndividualCI|BatchedCI|Manual|Schedule)['\"]\)",
                                cond, re.I)) or cond.strip() in ("false",)
        job["on_pr"] = trig["pr"] and not (tag_only or not_pr)
        job["on_tag"] = bool(trig["tags"]) or trig["branches"] == ["**"]
        job["on_main"] = bool(trig["branches"]) and not tag_only
        pl["jobs"].append(job)

    if isinstance(tree.get("stages"), list):
        stl = tree["stages"]
        for k, sg in enumerate(stl):
            if not isinstance(sg, dict):
                continue
            if "template" in sg and "stage" not in sg:
                f2, t2 = _az_template(root, file, sg["template"], seen)
                if t2 is not None:
                    pl["files"].append(f2)
                    for sg2 in _as_list(t2.get("stages") if isinstance(t2, dict) else None):
                        if isinstance(sg2, dict):
                            jl = sg2.get("jobs")
                            jll = getattr(jl, "ilines", [])
                            for kk, j in enumerate(_as_list(jl)):
                                add_job(j, f2, jll[kk] if kk < len(jll) else 1, sg2.get("stage"))
                continue
            jl = sg.get("jobs")
            jll = getattr(jl, "ilines", [])
            for kk, j in enumerate(_as_list(jl)):
                add_job(j, file, jll[kk] if kk < len(jll) else stl.ilines[k], sg.get("stage"),
                        str(sg.get("condition") or ""))
    elif isinstance(tree.get("jobs"), list):
        jl = tree["jobs"]
        for kk, j in enumerate(jl):
            add_job(j, file, jl.ilines[kk] if kk < len(jl.ilines) else 1)
    elif tree.get("steps"):
        add_job(YMap(1) if not isinstance(tree, YMap) else _az_single(tree), file, _kl(tree, "steps", 1))
    return pl


def _az_single(tree):
    j = YMap(tree.line)
    for k in ("steps", "pool", "container", "timeoutInMinutes", "strategy", "continueOnError"):
        if k in tree:
            j[k] = tree[k]
            j.klines[k] = _kl(tree, k)
    j["job"] = "build"
    return j


# =========================================================================== repository facts

LOCKFILES = {"package-lock.json": "npm", "npm-shrinkwrap.json": "npm", "pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn",
             "bun.lockb": "bun", "bun.lock": "bun", "uv.lock": "uv", "poetry.lock": "poetry", "Pipfile.lock": "pipenv",
             "pdm.lock": "pdm", "composer.lock": "composer", "go.sum": "go", "Cargo.lock": "cargo",
             "Gemfile.lock": "bundler", "gradle.lockfile": "gradle", "packages.lock.json": "nuget"}
MANIFESTS = ("package.json", "pyproject.toml", "requirements.txt", "setup.py", "Pipfile", "pom.xml", "build.gradle",
             "build.gradle.kts", "go.mod", "composer.json", "Cargo.toml", "Gemfile", "*.csproj")


def _walk(root: Path, max_depth: int = 4, limit: int = 20000):
    """Files under root, skipping dependency and build folders and anything git ignores is not checked (fast)."""
    root = Path(root)
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        depth = len(rel.parts)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not (d.startswith(".") and d not in
                                                                          (".github", ".azure-pipelines", ".gitlab"))]
        if depth >= max_depth:
            dirnames[:] = []
        for f in filenames:
            count += 1
            if count > limit:
                return
            yield rel / f


def facts(root) -> dict:
    """What the repository contains that the checks compare the pipeline with."""
    root = Path(root)
    files = list(_walk(root))
    top = {f.name for f in files if len(f.parts) == 1}
    f = {"root": str(root), "lockfiles": sorted(n for n in LOCKFILES if n in top),
         "pms": sorted({LOCKFILES[n] for n in LOCKFILES if n in top}), "top": top}
    text = {n: _read(root / n, 400_000) for n in ("package.json", "pyproject.toml", "setup.cfg", "tox.ini", "pom.xml",
                                                  "build.gradle", "build.gradle.kts", "composer.json", "go.mod",
                                                  "Makefile", "requirements.txt", "requirements-dev.txt",
                                                  ".pre-commit-config.yaml", "settings.gradle", "settings.gradle.kts")
            if (root / n).is_file()}
    f["text"] = text
    try:
        pkg = json.loads(text.get("package.json") or "{}")
    except ValueError:
        pkg = {}
    f["pkg"] = pkg if isinstance(pkg, dict) else {}
    f["scripts"] = f["pkg"].get("scripts") if isinstance(f["pkg"].get("scripts"), dict) else {}
    deps = {}
    for k in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        if isinstance(f["pkg"].get(k), dict):
            deps.update(f["pkg"][k])
    f["node_deps"] = deps
    py = text.get("pyproject.toml", "") + text.get("setup.cfg", "") + text.get("tox.ini", "")
    reqs = text.get("requirements.txt", "") + text.get("requirements-dev.txt", "")
    pom = text.get("pom.xml", "")
    gradle = text.get("build.gradle", "") + text.get("build.gradle.kts", "")
    f["linters"] = []
    for cond, name in (
            ("[tool.ruff" in py or {"ruff.toml", ".ruff.toml"} & top, "ruff"),
            ("[flake8]" in py or ".flake8" in top, "flake8"), ("[tool.pylint" in py or ".pylintrc" in top, "pylint"),
            ("[tool.black" in py, "black"),
            (any(n.startswith(("eslint.config.", ".eslintrc")) for n in top) or "eslintConfig" in f["pkg"], "eslint"),
            ({"biome.json", "biome.jsonc"} & top, "biome"), (".golangci.yml" in top or ".golangci.yaml" in top or
                                                             ".golangci.toml" in top, "golangci-lint"),
            ("spotless" in pom + gradle, "spotless"), ("checkstyle" in pom + gradle, "checkstyle"),
            ("ktlint" in gradle or "detekt" in gradle, "ktlint"),
            (any(n.startswith((".php-cs-fixer", "phpcs.xml", "pint.json")) for n in top), "php-cs"),
            ("lint" in f["scripts"], "npm run lint")):
        if cond:
            f["linters"].append(name)
    f["typecheckers"] = []
    for cond, name in (
            ("[tool.mypy" in py or "[mypy" in py or "mypy.ini" in top, "mypy"),
            ("[tool.pyright" in py or "pyrightconfig.json" in top or "[tool.basedpyright" in py, "pyright"),
            ("tsconfig.json" in top, "tsc"), ({"phpstan.neon", "phpstan.neon.dist", "phpstan.dist.neon"} & top, "phpstan"),
            ({"psalm.xml", "psalm.xml.dist"} & top, "psalm")):
        if cond:
            f["typecheckers"].append(name)
    f["reqs_text"] = reqs
    f["iac"] = sorted({str(x) for x in files if x.suffix in (".tf", ".bicep") or x.name in (
        "Chart.yaml", "kustomization.yaml", "kustomization.yml", "serverless.yml", "template.yaml", "cdk.json") or
        (x.parts and x.parts[0] in ("k8s", "kubernetes", "manifests", "deploy", "helm", "charts") and
         x.suffix in (".yaml", ".yml"))})[:20]
    f["dockerfiles"] = sorted(str(x) for x in files if x.name == "Dockerfile" or x.name.startswith("Dockerfile.")
                              or x.name.endswith(".Dockerfile") or x.name in ("Containerfile",))[:20]
    f["compose_test"] = sorted(str(x) for x in files if len(x.parts) <= 2 and re.match(
        r"^(docker-)?compose[.-](test|tests|ci|it|integration)\.ya?ml$", x.name))
    f["testcontainers"] = "testcontainers" in (" ".join(text.values()) + json.dumps(deps)).lower()
    test_files = [x for x in files if re.search(r"(^test_.*\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$|"
                                                r"(Test|Tests|IT)\.(java|kt)$|Test\.php$)", x.name)]
    f["test_files"] = len(test_files)
    parts = {}
    for x in files:
        if 1 <= len(x.parts) - 1 <= 2 and (x.name in MANIFESTS or x.suffix == ".csproj") and \
                not (x.name == "setup.py" and (root / x.parent / "__init__.py").exists()):
            parts.setdefault(str(x.parent), set()).add(x.name)
    root_ws = bool(f["pkg"].get("workspaces") or "pnpm-workspace.yaml" in top or "<modules>" in pom or
                   "include" in text.get("settings.gradle", "") + text.get("settings.gradle.kts", "") or
                   "go.work" in top or "[workspace]" in _read(root / "Cargo.toml", 100_000) or "lerna.json" in top or
                   "nx.json" in top or "turbo.json" in top)
    f["parts"] = sorted(parts)
    f["workspace"] = root_ws
    kinds = {frozenset(v) for v in parts.values()}
    f["monorepo"] = len(parts) >= 2 and (not root_ws or len(kinds) >= 2)
    f["updater"] = []
    dep = _read(root / ".github" / "dependabot.yml") or _read(root / ".github" / "dependabot.yaml")
    if dep:
        f["updater"].append("dependabot")
        f["dependabot_actions"] = "github-actions" in dep
    for n in ("renovate.json", "renovate.json5", ".renovaterc", ".renovaterc.json", ".github/renovate.json"):
        if (root / n).is_file():
            f["updater"].append("renovate")
    f.setdefault("dependabot_actions", False)
    f["has_deps"] = bool(f["lockfiles"]) or any(n in top for n in MANIFESTS if "*" not in n)
    return f


# =========================================================================== findings catalog

# id: (category, default severity, what it means)
CATALOG = {
    "no-pipeline": ("quality", "high", "no CI pipeline: nothing confirms a change before it lands"),
    "parse-error": ("reliability", "high", "a pipeline file cannot be read"),
    "no-dependency-cache": ("speed", "medium", "dependencies are downloaded again on every run"),
    "no-auto-cancel": ("speed", "medium", "superseded runs keep running after a newer push"),
    "full-ci-on-draft": ("speed", "low", "every job runs on draft pull requests"),
    "draft-gate-no-ready": ("reliability", "medium", "jobs skip drafts, but marking a draft ready starts no run"),
    "auto-cancel-unavailable": ("speed", "info", "the host cannot cancel superseded runs"),
    "no-path-filters": ("speed", "low", "every part of a monorepo is built for every change"),
    "no-test-sharding": ("speed", "low", "a long test job runs on one machine"),
    "no-junit-report": ("speed", "low", "no JUnit report: the host cannot show (or re-run) just the failed tests"),
    "tool-reinstall": ("speed", "low", "the same tools are installed again in several steps or jobs"),
    "fail-fast-implicit": ("speed", "low", "a matrix relies on the default fail-fast"),
    "no-job-timeout": ("speed", "medium", "a hung job runs until the host's limit"),
    "floating-image": ("reliability", "medium", "a container image without a fixed tag"),
    "runner-latest": ("reliability", "info", "a runner image label that moves (ubuntu-latest)"),
    "non-lockfile-install": ("reliability", "medium", "dependencies installed without the lockfile"),
    "unpinned-tool": ("reliability", "medium", "a tool installed without a fixed version"),
    "hidden-failure": ("reliability", "medium", "a failure that does not fail the pipeline"),
    "retry-masks-flakiness": ("reliability", "medium", "retries that hide flaky tests"),
    "yaml-float-version": ("reliability", "medium", "a version YAML reads as a number (3.10 becomes 3.1)"),
    "permissions-missing": ("security", "medium", "the GITHUB_TOKEN gets the repository's default permissions"),
    "permissions-broad": ("security", "high", "the GITHUB_TOKEN can write more than the jobs need"),
    "action-not-pinned": ("security", "high", "an action referenced by a tag or branch that can be moved"),
    "privileged-checkout": ("security", "critical", "a privileged trigger checks out and runs pull request code"),
    "script-injection": ("security", "high", "untrusted text pasted into a shell script"),
    "secret-exposure": ("security", "high", "a secret printed or passed on a command line"),
    "long-lived-cloud-keys": ("security", "medium", "long-lived cloud keys where OIDC is available"),
    "self-hosted-public": ("security", "high", "a self-hosted runner that public pull requests can reach"),
    "no-secret-scan": ("security", "medium", "no secret scanning in the pipeline"),
    "no-sast": ("security", "medium", "no static analysis (SAST) in the pipeline"),
    "no-sca": ("security", "medium", "no dependency vulnerability scanning (SCA) in the pipeline"),
    "no-iac-scan": ("security", "medium", "infrastructure as code is not scanned"),
    "no-container-scan": ("security", "medium", "container images are not scanned"),
    "image-push-unscanned": ("security", "high", "an image is pushed without a vulnerability scan"),
    "no-sbom": ("security", "medium", "releases ship without an SBOM"),
    "no-signing": ("security", "medium", "releases are neither signed nor attested"),
    "deploy-unprotected": ("security", "medium", "a deploy without a protected environment or approval"),
    "no-actions-updates": ("security", "low", "nothing keeps pinned actions up to date"),
    "no-pr-tests": ("quality", "high", "tests do not run on pull requests"),
    "no-lint": ("quality", "medium", "no lint step"),
    "no-typecheck": ("quality", "medium", "the type checker the project configures does not run"),
    "no-coverage": ("quality", "low", "test coverage is not reported"),
    "test-gate-mismatch": ("quality", "medium", "CI does not run the delivery policy's test gate"),
    "release-not-tag-driven": ("delivery", "medium", "releases are not driven by version tags"),
    "tag-format-mismatch": ("delivery", "medium", "tag triggers do not match the policy's tag format"),
    "release-without-tests": ("delivery", "medium", "a release publishes without running the test gate"),
    "publish-unverified": ("delivery", "low", "nothing checks the published artifact"),
}
AUTOFIX_IDS = ("permissions-missing", "no-job-timeout", "no-auto-cancel", "action-not-pinned", "full-ci-on-draft",
               "draft-gate-no-ready")


def finding(where: dict, id_: str, message: str, fix: str, line=None, severity=None, autofix=False, snippet=None,
            file=None, source="devkit") -> dict:
    cat, sev, _ = CATALOG.get(id_, ("reliability", "medium", ""))
    out = {"provider": where["provider"], "file": file or where.get("file"), "line": int(line or 1), "id": id_,
           "category": cat, "severity": severity or sev, "message": message, "fix": fix, "autofix": bool(autofix)}
    if snippet:
        out["snippet"] = snippet
    if source != "devkit":
        out["source"] = source
    return out


# =========================================================================== command patterns

def _rx(p):
    return re.compile(p, re.I | re.M)


TEST_RE = _rx(r"\b(pytest|py\.test|python3? -m (unittest|pytest)|unittest discover|tox\b|nox\b|vitest|jest\b|mocha\b|"
              r"\bava\b|phpunit|\bpest\b|rspec|ctest|cargo (test|nextest)|go test|gotestsum|dotnet test|vstest|"
              r"(mvnw?|mvn)\b[^\n]*\b(test|verify|install|integration-test)\b|gradlew?\b[^\n]*\b(test|check|build)\b|"
              r"(npm|pnpm|yarn|bun)\s+(run\s+)?(test|t)\b|make\s+(test|check)\b|composer\s+(run(-script)?\s+)?test\b|"
              r"bundle exec rake|rake (test|spec)|swift test|flutter test|node --test|deno test|playwright test|"
              r"cypress run|check\.(mjs|js|sh|py)\b|(npm|pnpm|yarn) run check\b|task:(Maven|Gradle|VSTest|DotNetCoreCLI)@|"
              r"\brun[_-]?tests?\.(py|sh|mjs|js|ps1)\b|\btests?\.sh\b|R CMD check|rcmdcheck|devtools::(test|check)|"
              r"testthat|ctest\b|meson test|ninja test|Invoke-Pester|-RunTests\b|build\.(cmd|sh|ps1)[^\n]*\s-{1,2}test\b|"
              r"PublishTestResults|publishJUnitResults|dorny/test-reporter|mikepenz/action-junit-report)")
LINT_RE = _rx(r"\b(ruff( check)?|flake8|pylint|black (--check|\.)|isort (--check|-c)|eslint|biome (check|lint|ci)|"
              r"prettier (--check|-c|--list-different)|stylelint|golangci-lint|go vet|staticcheck|gofmt -l|"
              r"checkstyle|spotless(:check|Check)|ktlint|detekt|phpcs|php-cs-fixer|pint --test|rubocop|shellcheck|"
              r"hadolint|markdownlint|yamllint|actionlint|(npm|pnpm|yarn|bun)\s+(run\s+)?lint\b|make lint|"
              r"cargo (clippy|fmt)|dotnet format|pre-commit run|lint\.(mjs|js|sh|py)\b|golangci/golangci-lint-action)")
TYPECHECK_RE = _rx(r"\b(mypy|pyright|basedpyright|pytype|ty check|tsc\b|vue-tsc|phpstan|psalm|"
                   r"(npm|pnpm|yarn|bun)\s+(run\s+)?(typecheck|type-check|types|check-types|tsc)\b|flow check|"
                   r"srb tc|next build|nuxi typecheck|svelte-check|astro check)")
COVERAGE_RE = _rx(r"(--cov\b|coverage (run|xml|report|json)|--coverage\b|\bc8\b|\bnyc\b|-coverprofile|jacoco|kover|"
                  r"--coverage-(clover|cobertura|xml|html|text)|XPlat Code Coverage|codecov|coveralls|simplecov|"
                  r"coverage_report|PublishCodeCoverageResults|--collect-coverage|coverage: true)")
JUNIT_RE = _rx(r"(--junit-?xml|junitxml|--reporter[= ]junit|reporters?[= ][^\n]*junit|jest-junit|--log-junit|"
               r"--junitfile|--test-reporter=junit|surefire-reports|failsafe-reports|test-results|test-reports|"
               r"TestResults|junit\.xml|TEST-[^\n]*\.xml|--logger[= ]\"?(trx|junit)|--format[= ]junit|-f junit|"
               r"--output-junit|junit_family|dorny/test-reporter|mikepenz/action-junit-report|"
               r"EnricoMi/publish-unit-test-result-action|PublishTestResults|publishJUnitResults|reports:\s*junit)")
SECRET_SCAN_RE = _rx(r"(gitleaks|trufflehog|detect-secrets|ggshield|git-secrets|secret[-_ ]?detection|kingfisher|"
                     r"noseyparker|talisman|trivy[^\n]*\b(fs|filesystem|repo)\b(?![^\n]*--scanners\s+(vuln|misconfig)"
                     r"(,(vuln|misconfig|license))*(\s|$))|CredScan|MicrosoftSecurityDevOps)")
SAST_RE = _rx(r"(semgrep|codeql|sonar|bandit|gosec|brakeman|spotbugs|find-sec-bugs|njsscan|snyk code|horusec|"
              r"\bSAST\b|Jobs/SAST|components/sast|MicrosoftSecurityDevOps|checkmarx|fortify|veracode|devskim|"
              r"bearer|psalm --taint|eslint-plugin-security)")
SCA_RE = _rx(r"(osv-scanner|trivy[^\n]*\b(fs|filesystem|repo|image|sbom)\b|grype|snyk (test|monitor)|npm audit|"
             r"pnpm audit|yarn (npm )?audit|pip-audit|safety (check|scan)|govulncheck|composer audit|"
             r"dependency-check|dependency-review-action|bundler-audit|bundle audit|cargo (audit|deny)|"
             r"Dependency-Scanning|components/dependency-scanning|ossindex|audit-ci|depscan|retire\b|"
             r"MicrosoftSecurityDevOps|whitesource|mend)")
IAC_RE = _rx(r"(trivy[^\n]*\b(config|misconfig)|checkov|tfsec|kics|terrascan|hadolint|kube-linter|kubeconform|"
             r"kubescape|conftest|SAST-IaC|IaC-SAST|template-analyzer|cfn-lint|cfn_nag|tflint|snyk iac|"
             r"MicrosoftSecurityDevOps)")
IMAGE_SCAN_RE = _rx(r"(trivy[^\n]*\bimage\b|grype|docker scout|snyk container|Container-Scanning|"
                    r"components/container-scanning|anchore/scan-action|aquasecurity/trivy-action|"
                    r"neuvector|prisma|clair|MicrosoftSecurityDevOps)")
SBOM_RE = _rx(r"(syft|sbom|cyclonedx|spdx|cdxgen|bom\.json)")
SIGN_RE = _rx(r"(cosign|sigstore|attest-build-provenance|actions/attest|slsa-github-generator|slsa-framework|"
              r"gpg[^\n]*(--detach-sign|--sign|--clearsign|\s-b\s|-sb\b)|--provenance|notation sign|in-toto|"
              r"jarsigner|signtool|minisign|signify|codesign|provenance:\s*(true|mode=max)|gh attestation|"
              r"maven-gpg-plugin|gpg:sign|signing\.key)")
PUBLISH_RE = _rx(r"(npm publish|pnpm publish|yarn (npm )?publish|twine upload|uv publish|poetry publish|"
                 r"flit publish|hatch publish|pypa/gh-action-pypi-publish|(mvnw?|mvn)\b[^\n]*\bdeploy\b|"
                 r"gradlew?\b[^\n]*\bpublish|docker push|docker buildx build[^\n]*--push|podman push|buildah push|"
                 r"crane push|\bko (build|publish)|kaniko|jib(:build)?\b|gh release (create|upload)|"
                 r"softprops/action-gh-release|ncipollo/release-action|goreleaser|cargo publish|gem push|"
                 r"nuget push|helm push|oras push|vsce publish|semantic-release|changesets/action|"
                 r"release-please-action|task:(Docker@\d+[^\n]*push|NuGetCommand|GitHubRelease|TwineAuthenticate)|"
                 r"(api/v4|CI_API_V4_URL)[^\n]*/(packages|releases)|/downloads\b[^\n]*(--form\b|-F\b)|"
                 r"release-cli create|glab release create)")
DOCKER_BUILD_RE = _rx(r"(docker (build|buildx build)|docker/build-push-action|podman build|buildah (bud|build)|"
                      r"kaniko|\bjib\b|task:Docker@)")
DOCKER_PUSH_RE = _rx(r"(docker push|--push\b|podman push|buildah push|crane push|kaniko|\bjib(:build)?\b|"
                     r"push:\s*true|buildAndPush)")
DEPLOY_RE = _rx(r"(kubectl (apply|rollout|set image|create|replace)|helm (upgrade|install)|terraform apply|"
                r"tofu apply|pulumi up|cdk deploy|sam deploy|serverless deploy|sls deploy|"
                r"aws (ecs update-service|lambda update-function-code|cloudformation deploy|deploy create-deployment)|"
                r"aws s3 sync|az (webapp|functionapp|containerapp|staticwebapp)[^\n]*(deploy|up|update|create)|"
                r"gcloud (run deploy|app deploy|functions deploy)|\bfly(ctl)? deploy|vercel[^\n]*--prod|"
                r"netlify deploy[^\n]*--prod|firebase deploy|\bcf push|ansible-playbook|argocd app sync|"
                r"flux reconcile|actions/deploy-pages|peaceiris/actions-gh-pages|JamesIves/github-pages-deploy-action|"
                r"azure/webapps-deploy|task:(AzureWebApp|AzureFunctionApp|KubernetesManifest|HelmDeploy|"
                r"AzureRmWebAppDeployment|AzureContainerApps)|mkdocs gh-deploy|pipe:[^\n]*(deploy|kubectl-run|ssh-run|"
                r"scp-deploy|rsync-deploy))")
VERIFY_RE = _rx(r"(gh release download|gh attestation verify|cosign verify|gpg[^\n]*--verify|sha256sum (-c|--check)|"
                r"shasum[^\n]*(-c|--check)|npm view|npm info|pip download|docker pull|crane (digest|manifest)|"
                r"curl -f|smoke|verify-signature|verify-blob|verify)")
AFFECTED_RE = _rx(r"(affected|changed-files|paths-filter|git diff[^\n]*--name-only|dorny/paths-filter|"
                  r"tj-actions/changed-files|nx affected|turbo[^\n]*--filter|--since\b|changesets|rules:\s*changes)")
SHARD_RE = _rx(r"(--shard|\s-n\s*(auto|\d+)|--numprocesses|xdist|availableParallelism|pytest-split|--splits?\b|split-tests|--maxWorkers|"
               r"-T\s*\d|--parallel\b|surefire\.parallel|forkCount|maxParallelForks|matrix\.shard|shardIndex|"
               r"CI_NODE_INDEX|BITBUCKET_PARALLEL_STEP|System\.JobPositionInPhase)")
RERUN_RE = _rx(r"(--reruns\b|--rerun-failing|rerunFailingTestsCount|--retries[= ]\d|jest\.retryTimes|--retry \d|"
               r"nick-fields/retry|Wandalen/wretry)")
GATE_RE = re.compile("|".join(r.pattern for r in (TEST_RE, LINT_RE, TYPECHECK_RE)), re.I | re.M)
SWALLOW_RE = re.compile(r"(\|\|\s*(true|:|exit 0)\b|;\s*true\s*$|\|\|\s*echo\b)")
SECRET_NAME_RE = re.compile(r"\$\{?([A-Z0-9_]*(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY|ACCESS_KEY)[A-Z0-9_]*)\}?")
CACHE_ACTIONS = ("actions/cache", "gradle/actions/setup-gradle", "gradle/gradle-build-action", "actions/setup-go",
                 "astral-sh/setup-uv", "ruby/setup-ruby", "mozilla-actions/sccache-action", "Swatinem/rust-cache",
                 "pnpm/action-setup@", "bahmutov/npm-install", "shivammathur/cache-extensions")
INSTALL_RE = _rx(r"(\bnpm (ci|install|i)\b|\bpnpm (install|i)\b|\byarn( install)?\s*($|&&|;|--)|\bbun install|"
                 r"\bpip3? install|-m pip install|\buv (sync|pip install)|\bpoetry install|\bpipenv (install|sync)|"
                 r"\bpdm (install|sync)|\bcomposer (install|update)|\bbundle( install)?\s*($|&&|;)|"
                 r"\bgo (mod download|build|test|vet)|\bcargo (build|test|fetch)|\bdotnet (restore|build|test)|"
                 r"(^|[\s;&(])(\./)?mvnw?\b|(^|[\s;&(])(\./)?gradlew?\b|\bsbt\b)")
DEPS_INSTALL_RE = _rx(r"(\bnpm (ci|install|i)\s*($|&&|;|\||--)|\bpnpm (install|i)\s*($|&&|;|\||--)|"
                      r"\byarn( install)?\s*($|&&|;|\||--)|\bbun install|pip3? install\s+(\S+\s+)*(-r|--requirement|-e|"
                      r"--editable|\.)\b|\buv sync|\buv pip (install|sync)\s+(\S+\s+)*(-r|-e|\.)|\bpoetry (install|sync)|"
                      r"\bpipenv (install|sync)|\bpdm (install|sync)|\bcomposer install|\bbundle( install)?\s*($|&&|;)|"
                      r"\bgo (mod download|build|test|vet)|\bcargo (build|test|fetch|nextest|clippy|check)|"
                      r"\bdotnet (restore|build|test)|(^|[\s;&(])(\./)?mvnw?\b|(^|[\s;&(])(\./)?gradlew?\b|\bsbt\b)")
PINNED_TOOLS_RE = _rx(r"^\s*(sudo\s+)?(python3?\s+-m\s+)?(uv\s+)?pip3?\s+install\s+(--\S+\s+)*"
                      r"([\w.\[\]-]+==[\w.+-]+\s*)+$")  # only pinned tools, not the project's dependencies
TOOL_INSTALL_RE = _rx(r"(apt-get install[^\n&|;]*|apt install[^\n&|;]*|apk add[^\n&|;]*|yum install[^\n&|;]*|"
                      r"dnf install[^\n&|;]*|brew install[^\n&|;]*|pip3? install (?!-r|-e|\.|--upgrade pip)[^\n&|;]*|"
                      r"npm (i|install) (-g|--global) [^\n&|;]*|go install [^\n&|;]*|gem install [^\n&|;]*|"
                      r"cargo install [^\n&|;]*|curl[^\n|]*\|\s*(sudo\s+)?(ba)?sh)")


# =========================================================================== context and helpers

class Ctx:
    """What one audit knows: the repository, its facts, the delivery policy and the files' lines."""

    def __init__(self, root, policy=None, visibility=None):
        self.root = Path(root)
        self.facts = facts(self.root)
        self.policy = policy or None
        self.visibility = visibility
        self._vis = "unknown"
        self.lines = {}

    def file_lines(self, rel: str) -> list:
        if rel not in self.lines:
            self.lines[rel] = _read(self.root / rel).splitlines()
        return self.lines[rel]

    def repo_visibility(self):
        if self._vis == "unknown":
            try:
                self._vis = self.visibility() if callable(self.visibility) else self.visibility
            except Exception:
                self._vis = None
        return self._vis


def _cmd_lines(step: dict, ctx: Ctx, file: str) -> list:
    """[(line number, text)] for each line of a step's script."""
    run = step.get("run")
    if not run:
        return []
    rows = run.rstrip("\n").split("\n")
    lines = ctx.file_lines(file)
    rl = step.get("run_line") or step["line"]
    raw = lines[rl - 1] if 0 < rl <= len(lines) else ""
    block = bool(re.search(r"[|>][-+0-9]*\s*(#.*)?$", raw.strip()))
    base = rl + 1 if block else rl
    return [(base + k, r) for k, r in enumerate(rows)]


def _direct(job: dict) -> str:
    if "_direct" not in job:
        parts = []
        for st in job["steps"]:
            if st.get("run"):
                parts.append(st["run"])
            if st.get("uses"):
                parts.append(st["uses"])
            if st.get("with"):
                parts.append(_text(st["with"]))
        parts += [i for i, _ in job["images"]]
        job["_direct"] = "\n".join(parts)
    return job["_direct"]


_NPM_BUILTIN = {"install", "i", "ci", "publish", "pack", "exec", "x", "audit", "view", "info", "version", "add",
                "remove", "config", "set", "cache", "dlx", "outdated", "update", "link", "login", "whoami", "init"}


def _expand(text: str, ctx: Ctx, depth: int = 0) -> str:
    """The commands plus what the project's own scripts they call contain (package.json, Makefile, files)."""
    if depth > 2 or not text:
        return ""
    f = ctx.facts
    out = []
    for m in re.finditer(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run(?:-script)?\s+)?(?:--\S+\s+)*([\w:.\-/]+)", text):
        name = m.group(1)
        if name in ("t",):
            name = "test"
        if name in f["scripts"] and name not in _NPM_BUILTIN:
            out.append(str(f["scripts"][name]))
            for pre in ("pre" + name, "post" + name):
                if pre in f["scripts"]:
                    out.append(str(f["scripts"][pre]))
    for m in re.finditer(r"\bcomposer\s+(?:run(?:-script)?\s+)?([\w:.\-]+)", text):
        try:
            sc = (json.loads(f["text"].get("composer.json") or "{}").get("scripts") or {}).get(m.group(1))
        except ValueError:
            sc = None
        if sc:
            out.append(_text(sc))
    mk = f["text"].get("Makefile", "")
    if mk:
        for m in re.finditer(r"\bmake\s+(?:-\S+\s+)*([\w.\-/]+(?:\s+[\w.\-/]+)*)", text):
            for target in m.group(1).split():
                mm = re.search(r"(?m)^" + re.escape(target) + r"\s*:[^\n]*\n((?:\t[^\n]*\n?)+)", mk)
                if mm:
                    out.append(mm.group(1))
    for m in re.finditer(r"(?:^|[\s;&|(])(?:node|python3?|bash|sh|zsh|deno run|tsx|ts-node|bun|ruby|php|pwsh|"
                         r"uv run(?: python)?|poetry run python)\s+(?:-\S+\s+)*([\w./\-]+\.(?:mjs|cjs|js|ts|py|sh|rb|php|ps1))"
                         r"|(?:^|[\s;&|(])(\./[\w./\-]+)", text):
        rel = re.sub(r"^(?:\./)+", "", m.group(1) or m.group(2) or "") if (m.group(1) or m.group(2) or "").startswith("./") \
            else (m.group(1) or "")
        pth = ctx.root / rel
        try:
            if rel and pth.is_file() and pth.stat().st_size < 300_000:
                out.append(_read(pth))
        except OSError:
            pass
    for m in re.finditer(r"(?:^|\s)[.$]/(\.github/workflows/[\w.-]+\.ya?ml|[\w./-]*actions/[\w.-]+)(?=\s|$)", text):
        pth = ctx.root / m.group(1)
        if pth.is_dir():
            pth = next((pth / n for n in ("action.yml", "action.yaml") if (pth / n).is_file()), pth)
        if pth.is_file():
            out.append(_read(pth, 300_000))
    if re.search(r"(^|[\s;&(])(\./)?mvnw?\b", text):
        out.append(f["text"].get("pom.xml", ""))
    if re.search(r"(^|[\s;&(])(\./)?gradlew?\b", text):
        out.append(f["text"].get("build.gradle", "") + f["text"].get("build.gradle.kts", ""))
    if re.search(r"\btox\b", text):
        out.append(f["text"].get("tox.ini", ""))
    if re.search(r"\bnox\b", text):
        out.append(_read(ctx.root / "noxfile.py", 200_000))
    if re.search(r"pre-commit run", text):
        out.append(f["text"].get(".pre-commit-config.yaml", ""))
    more = "\n".join(x for x in out if x)
    return more + ("\n" + _expand(more, ctx, depth + 1) if more else "")


def _corpus(job: dict, ctx: Ctx) -> str:
    if "_corpus" not in job:
        d = _direct(job)
        job["_corpus"] = d + "\n" + _expand(d, ctx)
    return job["_corpus"]


def _all(pls) -> list:
    return [j for pl in pls for j in pl["jobs"]]


def _is_first_party(owner: str) -> bool:
    return owner.lower() in ("actions", "github")


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def parse_uses(ref: str):
    """`owner/repo/path@ref` -> (owner, repo, path, ref) or None for local and docker references."""
    ref = (ref or "").strip().strip("\"'")
    if not ref or ref.startswith(("./", "$/", "docker://")) or "@" not in ref:
        return None
    name, _, ver = ref.rpartition("@")
    parts = name.split("/")
    if len(parts) < 2:
        return None
    return parts[0], parts[1], "/".join(parts[2:]), ver


def _image_floating(img: str) -> bool:
    img = img.strip().strip("\"'")
    if not img or "$" in img or "@sha256:" in img:
        return False
    last = img.rsplit("/", 1)[-1]
    if ":" not in last:
        return True
    return last.rsplit(":", 1)[1] in ("latest", "edge", "nightly", "main", "master", "stable", "current")


# =========================================================================== checks

def check_github(pl: dict, ctx: Ctx) -> list:
    out = []
    tree, trig = pl["tree"], pl["triggers"]
    if not isinstance(tree, dict):
        return out
    file = pl["file"]
    jobs = pl["jobs"]
    # --- permissions
    perms = pl.get("permissions")
    needs_write = {j["id"]: _write_needs(j) for j in jobs}
    if perms == "__missing__":
        uncovered = [j for j in jobs if j["permissions"] == "__missing__"]
        if uncovered:
            writers = {j["id"]: sorted(needs_write[j["id"]]) for j in uncovered if needs_write[j["id"]]}
            safe = not writers
            fix = ("add `permissions: contents: read` at the top of the workflow" if safe else
                   "add `permissions: contents: read` at the top, and on " +
                   ", ".join(f"job `{k}`: `permissions: {', '.join(v)}`" for k, v in writers.items()))
            out.append(finding(pl, "permissions-missing",
                               f"no `permissions:`: {len(uncovered)} job(s) get the repository's default token, which "
                               "may write to everything", fix, line=1, autofix=safe,
                               snippet="permissions:\n  contents: read"))
    elif perms == "write-all":
        out.append(finding(pl, "permissions-broad", "`permissions: write-all` gives every job a token that can write "
                           "everything", "set `permissions: contents: read` at the top and grant writes per job",
                           line=pl["permissions_line"]))
    elif isinstance(perms, dict):
        writes = [k for k, v in perms.items() if str(v) == "write"]
        if writes and len(jobs) > 1:
            out.append(finding(pl, "permissions-broad",
                               f"top-level write permissions ({', '.join(writes)}) apply to all {len(jobs)} jobs",
                               "keep `contents: read` at the top and move the writes to the job that needs them",
                               line=pl["permissions_line"]))
    for j in jobs:
        if j["permissions"] == "write-all":
            out.append(finding(pl, "permissions-broad", f"job `{j['id']}` has `permissions: write-all`",
                               "grant only the scopes this job uses", line=j["permissions_line"]))
    # --- cancel superseded runs
    conc = pl.get("concurrency")
    if trig["pr"]:
        if conc is None and not all(isinstance(j["map"].get("concurrency"), (dict, str)) for j in jobs if j.get("map")):
            out.append(finding(pl, "no-auto-cancel", "a pull request workflow without `concurrency`: every push to a "
                               "PR runs the whole workflow again while the previous run keeps going",
                               "add a `concurrency` group with `cancel-in-progress` for pull requests", line=_kl(tree, "on", 1),
                               autofix=True, snippet=_GH_CONCURRENCY))
        elif isinstance(conc, dict) and not conc.get("cancel-in-progress"):
            out.append(finding(pl, "no-auto-cancel", "`concurrency` without `cancel-in-progress`: newer runs wait "
                               "instead of replacing superseded ones",
                               "add `cancel-in-progress: ${{ github.event_name == 'pull_request' }}`",
                               line=_kl(tree, "concurrency", 1)))
    # --- jobs
    priv = trig["privileged"]
    latest = []
    for j in jobs:
        if not j["reusable"] and j["timeout"] is None:
            block = not _job_is_flow(ctx, file, j["line"])
            out.append(finding(j, "no-job-timeout", f"job `{j['id']}` has no `timeout-minutes` (GitHub's default is "
                               "6 hours)", f"add `timeout-minutes: {_default_timeout(ctx.policy)}`", line=j["line"],
                               autofix=block, file=file))
        if j["matrix"] and j["fail_fast"] is None:
            out.append(finding(j, "fail-fast-implicit", f"job `{j['id']}`: the matrix uses the default fail-fast "
                               "(true), so the first failing leg cancels the others",
                               "set `fail-fast:` explicitly, with a comment that says why (false only when every leg "
                               "tells you something different)", line=j["fail_fast_line"], file=file))
        for lab in [str(x) for x in _as_list(j["runs_on"])]:
            if lab.endswith("-latest"):
                latest.append((lab, j["runs_on_line"]))

        if j["self_hosted"]:
            vis = ctx.repo_visibility()
            if vis != "private" and (trig["pr"] or priv):
                out.append(finding(j, "self-hosted-public",
                                   f"job `{j['id']}` runs on a self-hosted runner and the workflow runs for pull "
                                   "requests" + (" in a public repository" if vis == "public" else
                                                 " (could not confirm the repository is private)"),
                                   "use GitHub-hosted runners for workflows that forks can trigger, or ephemeral, "
                                   "isolated runners with no secrets", line=j["runs_on_line"], file=file,
                                   severity="high" if vis == "public" else "low"))
        if j["environment"] is None and DEPLOY_RE.search(_direct(j)):
            out.append(finding(j, "deploy-unprotected", f"job `{j['id']}` deploys without an `environment`",
                               "add `environment: production` (or the right one) and protect it with required "
                               "reviewers and branch rules", line=j["line"], file=file))
        secret_env = {k for k, v in j["env"].items() if "secrets." in str(v)}
        for st in j["steps"]:
            out += _gh_step_checks(pl, j, st, priv, secret_env, ctx)
    if latest:
        labs = sorted({x for x, _ in latest})
        out.append(finding(pl, "runner-latest", f"runs on {', '.join(labs)}: the image moves to a new OS version "
                           "without a change in this repository", "fine for most projects; pin the "
                           "version (ubuntu-24.04, macos-15) when builds must be reproducible", line=latest[0][1]))
    out += _gh_draft_checks(pl)
    out += _yaml_float_versions(pl)
    return out


_GH_CONCURRENCY = ("concurrency:\n  group: ${{ github.workflow }}-${{ github.ref }}\n"
                   "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}")


def _default_timeout(policy) -> int:
    p = policy or {}
    for k in ("job_timeout_minutes", "timeout_minutes"):
        try:
            if p.get(k):
                return int(p[k])
        except (TypeError, ValueError):
            pass
    return 30


def _job_is_flow(ctx, file, line) -> bool:
    lines = ctx.file_lines(file)
    raw = lines[line - 1] if 0 < line <= len(lines) else ""
    return bool(re.search(r":\s*\{", raw))


def _write_needs(job: dict) -> set:
    """Scopes a job's steps need to write, judged from what they run (for safe `permissions` fixes)."""
    need = set()
    d = _direct(job)
    rules = ((r"gh release (create|upload|edit|delete)|git push|softprops/action-gh-release|ncipollo/release-action|"
              r"stefanzweifel/git-auto-commit-action|EndBug/add-and-commit|release-please-action|changesets/action|"
              r"goreleaser|gh api [^\n]*-X\s*(POST|PATCH|PUT|DELETE)[^\n]*(contents|git/|releases)", "contents: write"),
             (r"gh pr (comment|create|edit|merge|review|close)|peter-evans/create-pull-request|"
              r"sticky-pull-request-comment|actions/labeler|create-or-update-comment|actions-comment-pull-request|"
              r"add-pr-comment", "pull-requests: write"),
             (r"gh issue (create|comment|edit|close)|actions/stale|create-issue-from-file", "issues: write"),
             (r"docker push ghcr\.io|ghcr\.io[^\n]*--push|registry:\s*ghcr\.io|npm\.pkg\.github\.com",
              "packages: write"),
             (r"aws-actions/configure-aws-credentials|google-github-actions/auth|azure/login|actions/attest|"
              r"--provenance|pypa/gh-action-pypi-publish|sigstore/cosign-installer|cosign sign|"
              r"actions/deploy-pages|hashicorp/vault-action", "id-token: write"),
             (r"actions/attest", "attestations: write"), (r"actions/deploy-pages", "pages: write"),
             (r"dorny/test-reporter|mikepenz/action-junit-report|publish-unit-test-result-action", "checks: write"),
             (r"codeql-action/(analyze|upload-sarif)|upload-sarif", "security-events: write"),
             (r"gh (run (rerun|cancel)|workflow run|cache delete)", "actions: write"))
    for rx, scope in rules:
        if re.search(rx, d, re.I):
            need.add(scope)
    if "pull-requests: write" in need and "peter-evans/create-pull-request" in d:
        need.add("contents: write")
    return need


_DANGEROUS = re.compile(r"github\.(head_ref|event\.(issue\.(title|body)|pull_request\.(title|body|head\.(ref|label|"
                        r"repo\.default_branch))|comment\.body|review\.body|review_comment\.body|pages\.[^}]*?page_name|"
                        r"commits\.[^}]*?(message|author\.(email|name))|head_commit\.(message|author\.(email|name))|"
                        r"workflow_run\.(head_branch|head_commit\.(message|author\.(email|name))|display_title|"
                        r"pull_requests[^}]*?head\.ref)|discussion\.(title|body)))")
_SAFE_EVENT = re.compile(r"github\.event\.(number|before|after|ref|forced|action|repository\.|"
                         r"(pull_request|issue|release|workflow_run)\.(number|id|draft|merged|head\.sha|base\.sha|"
                         r"base\.ref|html_url|tag_name|created_at|run_attempt|conclusion|id)\b|sender\.(id|type)|"
                         r"inputs\.)")


def _gh_step_checks(pl, j, st, priv, secret_env, ctx) -> list:
    out = []
    file = pl["file"]
    uses = st.get("uses")
    if uses:
        out += _gh_pin_check(pl, j, st)
        u = uses.lower()
        w = st.get("with") or {}
        if priv and u.startswith("actions/checkout"):
            ref = str(w.get("ref") or "")
            if re.search(r"pull_request\.head|head_ref|workflow_run\.head|refs/pull/|merge_commit_sha", ref):
                out.append(finding(j, "privileged-checkout",
                                   f"`{'/'.join(priv)}` runs with a write token and secrets, and this step checks out "
                                   f"the pull request's code ({ref})", "split it: an unprivileged `pull_request` "
                                   "workflow runs the PR code; a `workflow_run` workflow only reads its results",
                                   line=st["uses_line"], file=file))
        if u.startswith("aws-actions/configure-aws-credentials") and ("aws-secret-access-key" in w or
                                                                     "aws-access-key-id" in w):
            out.append(finding(j, "long-lived-cloud-keys", "AWS access keys from secrets",
                               "use OIDC: `role-to-assume:` and `permissions: id-token: write`", line=st["uses_line"],
                               file=file))
        if u.startswith("google-github-actions/auth") and "credentials_json" in w:
            out.append(finding(j, "long-lived-cloud-keys", "a Google service account key from secrets",
                               "use Workload Identity Federation (`workload_identity_provider:`)", line=st["uses_line"],
                               file=file))
        if u.startswith("azure/login") and "creds" in w:
            out.append(finding(j, "long-lived-cloud-keys", "an Azure client secret (`creds:`) from secrets",
                               "use OIDC: `client-id`, `tenant-id`, `subscription-id` and `id-token: write`",
                               line=st["uses_line"], file=file))
        if u.startswith("actions/github-script"):
            sc = str(w.get("script") or "")
            for expr in re.findall(r"\$\{\{(.*?)\}\}", sc, re.S):
                if _DANGEROUS.search(expr):
                    out.append(finding(j, "script-injection", f"`${{{{{expr.strip()}}}}}` is pasted into a "
                                       "github-script body", "pass it through `env:` and read `process.env`",
                                       line=st["uses_line"], file=file,
                                       severity="critical" if priv else "high"))
    env_all = dict(j["env"], **(st.get("env") or {}))
    other_s3 = re.search(r"r2\.cloudflarestorage|digitaloceanspaces|backblazeb2|wasabisys|minio|storage\.googleapis",
                         str(env_all.get("AWS_ENDPOINT_URL") or env_all.get("AWS_S3_ENDPOINT") or "") +
                         str(st.get("run") or ""), re.I)
    for k, v in ({} if other_s3 else env_all).items():
        if re.match(r"^(AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID|GOOGLE_CREDENTIALS|GCP_SA_KEY|ARM_CLIENT_SECRET|"
                    r"AZURE_CLIENT_SECRET|GOOGLE_APPLICATION_CREDENTIALS_JSON)$", str(k)) and "secrets." in str(v):
            out.append(finding(j, "long-lived-cloud-keys", f"`{k}` comes from a long-lived secret",
                               "use the cloud's OIDC login action with `permissions: id-token: write`",
                               line=st["line"], file=file))
            break
    run = st.get("run")
    if not run:
        return out
    step_secret_env = secret_env | {k for k, v in (st.get("env") or {}).items() if "secrets." in str(v)}
    for ln, row in _cmd_lines(st, ctx, file):
        for expr in re.findall(r"\$\{\{(.*?)\}\}", row):
            e = expr.strip()
            if _DANGEROUS.search(e):
                out.append(finding(j, "script-injection", f"`${{{{ {e} }}}}` is pasted into the shell script: anyone "
                                   "who can set it can run commands" + (f" with `{'/'.join(priv)}` privileges" if priv
                                                                         else ""),
                                   "move it to `env:` (NAME: ${{ … }}) and use \"$NAME\" in the script", line=ln,
                                   file=file, severity="critical" if priv else "high"))
            elif "github.event." in e and not _SAFE_EVENT.search(e):
                out.append(finding(j, "script-injection", f"`${{{{ {e} }}}}` is pasted into the shell script",
                                   "pass event data through `env:` and quote it", line=ln, file=file,
                                   severity="low"))
            if re.search(r"\bsecrets\.", e) or "toJSON(secrets)" in e.replace(" ", ""):
                printed = re.search(r"\b(echo|printf|cat)\b", row) and not re.search(r"\|\s*\S|>\s*\S", row)
                out.append(finding(j, "secret-exposure",
                                   f"`${{{{ {e} }}}}` is " + ("printed to the log" if printed else
                                                              "put on a command line (process list, logs with set -x)"),
                                   "pass it through `env:` and let the tool read the variable (or --password-stdin)",
                                   line=ln, file=file, severity="high" if printed else "medium"))
        for var in step_secret_env:
            if re.search(r"\b(echo|printf)\b[^|>\n]*\$\{?" + re.escape(var) + r"\b", row) and \
                    not re.search(r"\|\s*\S|>\s*\S", row):
                out.append(finding(j, "secret-exposure", f"`${var}` holds a secret and is printed to the log",
                                   "never print secrets; pipe them to the tool (`… | tool --password-stdin`)",
                                   line=ln, file=file))
        if step_secret_env and re.search(r"(^|\s)set -[a-z]*x|bash -[a-z]*x", row):
            out.append(finding(j, "secret-exposure", "`set -x` traces commands in a step that has secrets in its "
                               "environment", "remove `set -x` from steps that handle secrets", line=ln, file=file,
                               severity="medium"))
        if priv and "pull_request_target" in priv and re.search(r"gh pr checkout|git fetch[^\n]*pull/", row):
            out.append(finding(j, "privileged-checkout", "`pull_request_target` fetches the pull request's code in a "
                               "job with a write token and secrets", "run PR code only in `pull_request` workflows",
                               line=ln, file=file))
    return out


def _gh_pin_check(pl, j, st) -> list:
    parsed = parse_uses(st["uses"])
    if not parsed:
        if st["uses"].startswith("docker://") and _image_floating(st["uses"][9:]):
            return [finding(j, "floating-image", f"`{st['uses']}` has no fixed tag", "pin a tag and digest",
                            line=st.get("uses_line") or st["line"], file=pl["file"])]
        return []
    owner, repo, path, ver = parsed
    if _SHA_RE.match(ver):
        return []
    branchy = ver in ("main", "master", "develop", "dev", "trunk", "HEAD", "latest", "stable", "nightly") or \
        not re.match(r"^v?\d", ver)
    first = _is_first_party(owner)
    sev = "high" if (branchy or not first) else "low"
    what = "a branch, which changes with every push" if branchy else "a tag, which its owner can move"
    return [finding(j, "action-not-pinned",
                    f"`{owner}/{repo}{'/' + path if path else ''}@{ver}` points at {what}" +
                    ("" if first else " (third-party code runs with this job's token and secrets)"),
                    "pin the full commit SHA with the version in a comment: `uses: owner/repo@<sha> # vX.Y.Z` "
                    "(`dev ci audit --fix` does it)", line=st.get("uses_line") or st["line"], file=pl["file"],
                    severity=sev, autofix=True)]


def _yaml_float_versions(pl) -> list:
    out = []
    for k, raw in enumerate(pl["text"].splitlines(), 1):
        m = re.match(r"^\s*-?\s*([\w-]*(version|python|node|go|java|php|ruby|dotnet)[\w-]*)\s*:\s*(.+)$", raw, re.I)
        if not m:
            continue
        val = _plain_cut(m.group(3))
        for num in re.findall(r"(?:^|[\[,\s])(\d+\.\d*0)(?=$|[\],\s])", val):
            out.append(finding(pl, "yaml-float-version", f"`{m.group(1)}: {val}` — YAML reads {num} as a number, so "
                               f"the tool sees {float(num):g}", f"quote it: \"{num}\"", line=k))
            break
    return out


def check_gitlab(pl: dict, ctx: Ctx) -> list:
    out = []
    for f, err in pl.get("errors", []):
        out.append(finding(pl, "parse-error", f"cannot read the included file: {err.msg}", "fix the YAML",
                           line=err.line, file=f))
    missing = [inc[8:] for inc in pl.get("remote_includes", []) if inc.startswith("missing:")]
    if missing:
        out.append(finding(pl, "parse-error", f"{len(missing)} included file(s) not found: {', '.join(missing[:4])}"
                           f"{'…' if len(missing) > 4 else ''} (their jobs are not audited)", "fix the include paths",
                           line=_kl(pl["tree"], "include", 1), severity="medium"))
    jobs = pl["jobs"]
    if not jobs:
        return out
    default = pl.get("default") or {}
    mr_jobs = [j for j in jobs if j["on_pr"] and not j["environment"]]
    if mr_jobs and not any(j.get("interruptible") is True for j in mr_jobs):
        out.append(finding(pl, "no-auto-cancel", "no job is `interruptible`: GitLab cannot cancel a pipeline that a "
                           "newer push makes redundant", "mark build and test jobs interruptible (not deploys), "
                           "and keep the project's auto-cancel setting on", line=1,
                           snippet="default:\n  interruptible: true"))
    no_to = [j for j in jobs if not j["timeout"]]
    if no_to and not default.get("timeout"):
        out.append(finding(no_to[0], "no-job-timeout", f"{len(no_to)} job(s) without `timeout:` (the project "
                           "default applies, 1 hour unless changed)", "set a default timeout and raise it only where "
                           "needed", line=no_to[0]["line"], severity="low", snippet="default:\n  timeout: 30m"))
    for j in jobs:

        for val, when, ln in j["retry"]:
            if not when:
                out.append(finding(j, "retry-masks-flakiness", f"job `{j['id']}` retries {val}× on any failure, "
                                   "including real test failures", "retry only infrastructure failures",
                                   line=ln, snippet="retry:\n  max: 2\n  when: [runner_system_failure, "
                                   "stuck_or_timeout_failure, scheduler_failure]"))
        if j["environment"] is None and DEPLOY_RE.search(_direct(j)):
            out.append(finding(j, "deploy-unprotected", f"job `{j['id']}` deploys without an `environment`",
                               "add an environment and protect it (Settings → CI/CD → Protected environments), "
                               "with `when: manual` for production", line=j["line"],
                               snippet="environment:\n  name: production\nwhen: manual"))
    bs = default.get("before_script")
    if bs and len(jobs) >= 3 and TOOL_INSTALL_RE.search(_text(bs)):
        out.append(finding(pl, "tool-reinstall", f"`before_script` installs tools in every one of {len(jobs)} jobs",
                           "bake the tools into a pinned image, or install them only in the jobs that need them",
                           line=_kl(default, "before_script", 1)))
    return out


def check_bitbucket(pl: dict, ctx: Ctx) -> list:
    out = []
    if pl["jobs"]:
        out.append(finding(pl, "auto-cancel-unavailable", "Bitbucket Pipelines has no automatic cancel of superseded "
                           "runs: every push runs to the end", "keep pull request pipelines short (cache, parallel "
                           "steps, path conditions) and stop stale runs by hand", line=1))
    no_to = [j for j in pl["jobs"] if not j["timeout"]]
    if no_to:
        out.append(finding(no_to[0], "no-job-timeout", f"{len(no_to)} step(s) without `max-time` (default 120 "
                           "minutes)", "set a global limit", line=no_to[0]["line"], severity="low",
                           snippet="options:\n  max-time: 30"))
    for j in pl["jobs"]:

        for st in j["steps"]:
            u = st.get("uses") or ""
            if u.startswith("pipe:"):
                pipe = u[5:].strip()
                tag = pipe.rsplit(":", 1)[1] if ":" in pipe.rsplit("/", 1)[-1] else ""
                if not tag or tag == "latest":
                    out.append(finding(j, "floating-image", f"pipe `{pipe}` has no fixed version",
                                       "pin the full version (name:1.2.3)", line=st["line"]))
                elif not re.match(r"^\d+\.\d+\.\d+", tag) and not pipe.startswith("docker://"):
                    out.append(finding(j, "unpinned-tool", f"pipe `{pipe}` follows a major version",
                                       "pin the full version (name:1.2.3)", line=st["line"], severity="low"))
        if j["environment"] is None and DEPLOY_RE.search(_direct(j)):
            out.append(finding(j, "deploy-unprotected", f"step “{j['name']}” deploys without `deployment:`",
                               "add `deployment: production` (restrict it in Repository settings → Deployments) and "
                               "`trigger: manual`", line=j["line"], snippet="deployment: production\ntrigger: manual"))
    return out


def check_azure(pl: dict, ctx: Ctx) -> list:
    out = []
    trig = pl["triggers"]
    if trig["pr"] and not trig["auto_cancel"]:
        out.append(finding(pl, "no-auto-cancel", "`pr: autoCancel: false` keeps superseded PR runs going",
                           "remove it (the default cancels them)", line=pl.get("auto_cancel_line") or 1,
                           snippet="pr:\n  autoCancel: true"))
    if trig["branches"] and not trig["batch"] and pl["jobs"]:
        out.append(finding(pl, "no-auto-cancel", "CI runs for every push to the branch; pushes in a row queue "
                           "full runs", "batch them: `trigger: batch: true`", line=pl.get("trigger_line") or 1,
                           severity="low", snippet="trigger:\n  batch: true\n  branches:\n    include: [main]"))
    no_to = [j for j in pl["jobs"] if not j["timeout"]]
    if no_to:
        out.append(finding(no_to[0], "no-job-timeout", f"{len(no_to)} job(s) without `timeoutInMinutes` (default "
                           "60)", "set it per job", line=no_to[0]["line"], severity="low",
                           snippet="timeoutInMinutes: 30"))
    latest = []
    for j in pl["jobs"]:
        if str(j["runs_on"] or "").endswith("-latest"):
            latest.append((j["runs_on"], j["runs_on_line"]))

        for val, _, ln in j["retry"]:
            out.append(finding(j, "retry-masks-flakiness", f"job `{j['id']}` retries failed tasks {val}×",
                               "retry only steps that fail for infrastructure reasons, never tests", line=ln,
                               severity="low"))
        if not j.get("deployment") and DEPLOY_RE.search(_direct(j)):
            out.append(finding(j, "deploy-unprotected", f"job `{j['id']}` deploys from a plain job",
                               "use a `deployment:` job with an `environment:` that has approvals and checks",
                               line=j["line"], snippet="- deployment: deploy\n  environment: production\n  strategy:\n"
                                                       "    runOnce:\n      deploy:\n        steps: [...]"))
    if latest:
        out.append(finding(pl, "runner-latest", f"vmImage {latest[0][0]} moves to a new OS version on its own",
                           "fine for most projects; pin the version when builds must be reproducible",
                           line=latest[0][1]))
    out += _yaml_float_versions(pl)
    return out


def check_jobs(pl: dict, ctx: Ctx) -> list:
    """Checks that read what jobs run, the same on every host."""
    out = []
    f = ctx.facts
    floating = {}
    for j in pl["jobs"]:
        for img, ln in j["images"]:
            if _image_floating(img):
                floating.setdefault((j["file"], img), []).append((ln, j))
    for (file, img), uses in floating.items():
        ln, j = min(uses, key=lambda x: x[0])
        names = sorted({x[1]["name"] for x in uses})
        who = f"job `{names[0]}`" if len(names) == 1 else f"{len(names)} jobs ({', '.join(names[:3])}{'…' if len(names) > 3 else ''})"
        out.append(finding(j, "floating-image", f"{who} run in `{img}`, which has no fixed tag and moves",
                           "use a fixed tag, better a digest (name:tag@sha256:…)", line=ln, file=file,
                           snippet=f"image: {img.rsplit(':', 1)[0] if ':' in img.rsplit('/', 1)[-1] else img}:<version>@sha256:<digest>"
                           if pl["provider"] != "github" else None))
    installs_seen = {}
    for j in pl["jobs"]:
        file = j["file"]
        d = _direct(j)
        cached = j["cache"] or _gh_cached(j) or ("task:Cache@" in d)
        release_only = not (j["on_pr"] or j["on_main"])  # a cache in a release job invites cache poisoning
        deps_rows = [r for st in j["steps"] if st.get("run") for r in st["run"].split("\n")
                     if DEPS_INSTALL_RE.search(r) and not PINNED_TOOLS_RE.search(r)]
        opted_out = _cache_opted_out(j)
        if not cached and not release_only and not opted_out and deps_rows:
            snip = {"gitlab": "cache:\n  key:\n    files: [<lockfile>]\n  paths: [<dependency folder>]",
                    "bitbucket": "caches:\n  - node   # or pip, maven, gradle, composer, or a custom cache",
                    "azure": "- task: Cache@2\n  inputs:\n    key: '\"deps\" | \"$(Agent.OS)\" | <lockfile>'\n"
                             "    path: <dependency folder>"}.get(pl["provider"])
            fix = {"github": "turn on the setup action's cache (`cache: npm|pip|maven|gradle`, setup-uv "
                             "`enable-cache: true`) or add actions/cache keyed on the lockfile"}.get(
                pl["provider"], "cache the dependency folder, keyed on the lockfile")
            out.append(finding(j, "no-dependency-cache", f"job `{j['name']}` installs dependencies without a cache",
                               fix, line=j["line"], file=file, snippet=snip))
        per_step = {}
        for st in j["steps"]:
            for ln, row in _cmd_lines(st, ctx, file):
                out += _row_checks(pl, j, ln, row, ctx, file)
                for m in TOOL_INSTALL_RE.finditer(row):
                    sig = re.sub(r"\s+", " ", m.group(0).strip())
                    per_step.setdefault(sig, set()).add(id(st))
                    installs_seen.setdefault(sig, set()).add((j["file"], j["name"]))
        dup = [sig for sig, steps in per_step.items() if len(steps) >= 2]
        if dup:
            out.append(finding(j, "tool-reinstall", f"job `{j['name']}` installs the same tool in {len(per_step[dup[0]])} "
                               f"steps: `{dup[0][:70]}`", "install it once per job (or bake it into the image)",
                               line=j["line"], file=file))
        for hf in j["hidden_failure"]:
            desc, ln, own = hf[0], hf[1], (hf[2] if len(hf) > 2 else None)
            if not _is_gate(d if own is None else own):
                continue  # optional work (notifications, uploads) may fail without failing the pipeline
            out.append(finding(j, "hidden-failure", f"job `{j['name']}`: {desc} lets a failure pass unnoticed",
                               "remove it from gates; for optional work, report the result instead of hiding it",
                               line=ln, file=file))
        if TEST_RE.search(d) and RERUN_RE.search(d):
            out.append(finding(j, "retry-masks-flakiness", f"job `{j['name']}` re-runs failing tests",
                               "fix or quarantine flaky tests instead (`testing` skill)", line=j["line"], file=file,
                               severity="low"))
        corpus = _corpus(j, ctx)
        if j["on_pr"] and TEST_RE.search(corpus) and not (j["junit"] or _junit(j, corpus, pl["provider"])):
            out.append(finding(j, "no-junit-report", f"job `{j['name']}` runs tests without a JUnit report",
                               _JUNIT_FIX.get(pl["provider"]), line=j["line"], file=file,
                               snippet=_JUNIT_SNIP.get(pl["provider"])))
        long_job = isinstance(j["timeout"], int) and j["timeout"] >= 45 or \
            isinstance(j["timeout"], str) and _minutes(j["timeout"]) >= 45
        if TEST_RE.search(corpus) and not (j["matrix"] or j["parallel"] or SHARD_RE.search(corpus)) and \
                (long_job or f["test_files"] >= 300):
            out.append(finding(j, "no-test-sharding", f"job `{j['name']}` runs " +
                               (f"{f['test_files']} test files" if f["test_files"] >= 300 else
                                f"tests with a {_minutes(j['timeout'])}-minute timeout") + " on one machine, one "
                               "process at a time",
                               "split the suite into shards (matrix / parallel: N) and keep slow end-to-end tests in "
                               "their own job", line=j["line"], file=file))
    multi = [sig for sig, jobs_ in installs_seen.items() if len(jobs_) >= 3 and not sig.startswith("curl")]
    if multi:
        out.append(finding(pl, "tool-reinstall", f"{len(installs_seen[multi[0]])} jobs install `{multi[0][:60]}`",
                           "bake shared tools into a pinned image or cache them", line=1))
    return out


_JUNIT_FIX = {"github": "write JUnit XML (pytest --junitxml, vitest --reporter=junit, surefire) and upload it with "
                        "actions/upload-artifact (`if: ${{ !cancelled() }}`)",
              "gitlab": "write JUnit XML and add `artifacts: reports: junit:` so the merge request shows each failed test",
              "bitbucket": "write JUnit XML into a test-results/ folder; Bitbucket finds it there and shows the failed "
                           "tests",
              "azure": "write JUnit XML and add PublishTestResults@2 (condition: succeededOrFailed())"}
_JUNIT_SNIP = {"gitlab": "artifacts:\n  when: always\n  reports:\n    junit: test-results/junit.xml",
               "azure": "- task: PublishTestResults@2\n  condition: succeededOrFailed()\n  inputs:\n"
                        "    testResultsFormat: JUnit\n    testResultsFiles: '**/junit*.xml'"}


def _minutes(v) -> int:
    s = str(v).lower()
    tot = 0
    for num, unit in re.findall(r"(\d+)\s*(h|hour|hours|m|min|mins|minute|minutes|s|sec|seconds)?", s):
        n = int(num)
        tot += n * 60 if unit.startswith("h") else n // 60 if unit.startswith("s") else n
    return tot


def _cache_opted_out(j: dict) -> bool:
    """The author turned caching off on purpose (cache: "", enable-cache: false, sccache: false, cache-disabled)."""
    for st in j["steps"]:
        w = st.get("with") or {}
        for k in ("cache", "enable-cache", "sccache", "package-manager-cache"):
            if k in w and str(w[k]).strip().lower() in ("", "false", "none"):
                return True
        if str(w.get("cache-disabled", "")).lower() == "true":
            return True
    return False


def _is_gate(text: str) -> bool:
    return bool(GATE_RE.search(text) or SECRET_SCAN_RE.search(text) or SAST_RE.search(text) or SCA_RE.search(text)
                or IMAGE_SCAN_RE.search(text) or DOCKER_BUILD_RE.search(text) or INSTALL_RE.search(text))


def _gh_cached(j: dict) -> bool:
    if j["provider"] != "github":
        return False
    for st in j["steps"]:
        u = (st.get("uses") or "").lower()
        w = st.get("with") or {}
        if any(u.startswith(a.lower()) for a in CACHE_ACTIONS) or re.search(r"cache", u.split("@")[0]):
            if u.startswith(("actions/setup-go", "astral-sh/setup-uv")) and str(w.get("cache", w.get("enable-cache"))).lower() == "false":
                continue
            if u.startswith("pnpm/action-setup"):
                continue
            return True
        if u.startswith(("actions/setup-node", "actions/setup-python", "actions/setup-java", "actions/setup-dotnet")) \
                and w.get("cache"):
            return True
        if u.startswith("ruby/setup-ruby") and w.get("bundler-cache"):
            return True
    return False


def _junit(j: dict, corpus: str, provider: str) -> bool:
    if provider == "github":
        uploaded = any((st.get("uses") or "").startswith("actions/upload-artifact") and
                       re.search(r"junit|test-results|surefire|failsafe|reports|TEST-", _text(st.get("with")), re.I)
                       for st in j["steps"])
        reporters = re.search(r"dorny/test-reporter|mikepenz/action-junit-report|publish-unit-test-result", corpus)
        return bool(reporters or uploaded and JUNIT_RE.search(corpus))
    if provider == "bitbucket":
        return bool(re.search(r"test-results|test-reports|surefire-reports|failsafe-reports|TestResults", corpus) or
                    re.search(r"(^|[\s;&(])(\./)?(mvnw?|gradlew?)\b", corpus))
    if provider == "azure":
        return bool(re.search(r"PublishTestResults|task:(Maven|Gradle|DotNetCoreCLI|VSTest)@", corpus))
    return False


_PIP_OPTS_WITH_VALUE = {"--group", "-c", "--constraint", "-i", "--index-url", "--extra-index-url", "-f",
                        "--find-links", "-t", "--target", "--prefix", "--root", "--python-version", "--platform",
                        "--implementation", "--abi", "--src", "-C", "--config-settings", "--report", "--cache-dir",
                        "--trusted-host", "--progress-bar", "--python", "-p", "--only-binary", "--no-binary",
                        "--upgrade-strategy", "--log", "--proxy", "--retries", "--timeout", "--exists-action"}


def _row_checks(pl, j, ln, row, ctx, file) -> list:
    """One line of a script: lockfile installs, unpinned tools, swallowed failures, printed secrets."""
    out = []
    f = ctx.facts
    r = row.strip()
    if not r or r.startswith("#"):
        return out

    def add(id_, msg, fix, sev=None, snippet=None):
        out.append(finding(j, id_, msg, fix, line=ln, file=file, severity=sev, snippet=snippet))

    for seg in re.split(r"&&|\|\||;|\|", r):
        c = seg.strip()
        m = re.match(r"^(?:sudo\s+)?npm\s+(install|i)\b(.*)$", c)
        if m:
            args = [a for a in m.group(2).split() if not a.startswith("-")]
            if re.search(r"(^|\s)(-g|--global)\b", m.group(2)):
                bad = [a for a in args if not re.search(r"@\d", a)]
                if bad:
                    add("unpinned-tool", f"`npm install -g {' '.join(bad)}` installs whatever version is newest",
                        f"pin it: `npm install -g {bad[0]}@<version>`")
            elif not args:
                add("non-lockfile-install", "`npm install` can change the lockfile and resolve new versions",
                    "use `npm ci`", snippet="npm ci")
            continue
        if re.match(r"^yarn(\s+install)?(\s+--?[\w-]+)*\s*$", c) and "--frozen-lockfile" not in c and "--immutable" not in c:
            berry = ".yarnrc.yml" in f["top"]
            add("non-lockfile-install", "`yarn install` without a frozen lockfile",
                "use `yarn install --immutable` (Yarn 2+) or `--frozen-lockfile` (Yarn 1)", "low" if berry else None)
            continue
        if re.match(r"^pnpm\s+(install|i)\b", c) and "--frozen-lockfile" not in c and not re.search(r"pnpm (install|i)\s+\w", c):
            add("non-lockfile-install", "`pnpm install` without `--frozen-lockfile` (pnpm freezes it in CI, but say so)",
                "use `pnpm install --frozen-lockfile`", "low")
            continue
        if re.match(r"^uv\s+sync\b", c) and not re.search(r"--(frozen|locked)\b", c):
            add("non-lockfile-install", "`uv sync` without `--frozen` or `--locked` can update uv.lock",
                "use `uv sync --frozen` (or `--locked` to fail when the lock is stale)", snippet="uv sync --frozen")
            continue
        if re.match(r"^poetry\s+install\b", c) and "poetry.lock" not in f["top"]:
            add("non-lockfile-install", "`poetry install` without a committed poetry.lock",
                "commit poetry.lock (`poetry lock`)")
            continue
        if re.match(r"^composer\s+update\b", c):
            add("non-lockfile-install", "`composer update` resolves new versions in CI", "use `composer install`")
            continue
        if re.match(r"^composer\s+install\b", c) and "composer.lock" not in f["top"]:
            add("non-lockfile-install", "`composer install` without a committed composer.lock", "commit composer.lock")
            continue
        m = re.match(r"^(?:sudo\s+)?(?:python3?\s+-m\s+)?(?:uv\s+)?pip3?\s+install\b(.*)$", c)
        if m:
            args = m.group(1)
            if "--require-hashes" in args or re.search(r"(^|\s)(-c|--constraint)\s", args):
                continue
            reqs = re.findall(r"(?:-r|--requirement)\s*(\S+)", args)
            for rq in reqs:
                txt = _read(ctx.root / rq)
                loose = [x.strip() for x in txt.splitlines() if x.strip() and not x.strip().startswith(("#", "-"))
                         and "==" not in x and " @ " not in x and not x.strip().startswith(("http", "git+"))]
                if txt and loose:
                    add("non-lockfile-install", f"`{rq}` has unpinned requirements ({', '.join(loose[:3])}"
                        f"{'…' if len(loose) > 3 else ''}): each run can resolve different versions",
                        "lock them: `uv pip compile --generate-hashes` (or pip-compile) and install with "
                        "`--require-hashes`, or move to uv/poetry with a lockfile")
            rest = re.sub(r"(?:-r|--requirement)\s*\S+", "", args)
            toks, editable, skip = [], False, False
            for t in rest.split():
                if skip:
                    skip = False
                    continue
                if t in _PIP_OPTS_WITH_VALUE:
                    skip = True
                    continue
                if t.startswith(("-e", "--editable")):
                    editable = True
                    skip = t in ("-e", "--editable")
                    continue
                if not t.startswith("-"):
                    toks.append(t)
            if editable or any(t in (".", "./") or t.startswith((".[", "./[")) for t in toks):
                if not (set(f["lockfiles"]) & {"uv.lock", "poetry.lock", "Pipfile.lock", "pdm.lock"}):
                    add("non-lockfile-install", "installs the project's dependencies without a lock: each run can get "
                        "different versions", "add a lockfile (uv lock) and install from it (`uv sync --frozen`)")
            tools = [t for t in toks if t not in (".", "./") and not t.startswith((".", "/", "$", "http", "git+"))
                     and not re.search(r"[=<>~!]=|@", t) and t.lower() not in ("pip", "setuptools", "wheel", "--upgrade")]
            if tools:
                add("unpinned-tool", f"`pip install {' '.join(tools)}` installs whatever version is newest",
                    f"pin it: `{tools[0]}==<version>`")
            continue
        m = re.match(r"^(?:pipx\s+(?:install|run)|uv\s+tool\s+(?:install|run)|uvx)\s+(?:--\S+\s+)*(\S+)", c)
        if m and not re.search(r"[=@]\d|==", m.group(1)) and m.group(1) not in ("--from",):
            add("unpinned-tool", f"`{c[:60]}` runs whatever version is newest", f"pin it: `{m.group(1)}@<version>` "
                "(uvx) or `{m.group(1)}==<version>` (pipx)".replace("{m.group(1)}", m.group(1)), "low")
            continue
        m = re.match(r"^go\s+install\s+(\S+)", c)
        if m and (m.group(1).endswith("@latest") or "@" not in m.group(1)):
            add("unpinned-tool", f"`go install {m.group(1)}` takes the newest version", "pin it: `…@vX.Y.Z`")
            continue
        m = re.match(r"^(?:cargo\s+install|gem\s+install|dotnet\s+tool\s+install)\s+(.*)$", c)
        if m and not re.search(r"--version|\s-v\s|@\d", m.group(1)):
            add("unpinned-tool", f"`{c[:60]}` installs the newest version", "pin it with `--version`")
            continue
        m = re.match(r"^npx\s+(?:--yes\s+|-y\s+)?([@\w][\w./@-]*)", c)
        if m:
            pkg = m.group(1)
            base = pkg.rsplit("@", 1)[0] if pkg.count("@") > (1 if pkg.startswith("@") else 0) else pkg
            if base not in f["node_deps"] and not re.search(r"@\d", pkg) and base not in ("--no-install",):
                add("unpinned-tool", f"`npx {pkg}` downloads the newest `{base}` (it is not in package.json)",
                    f"add it to devDependencies, or pin it: `npx {base}@<version>`", "low")
    if re.search(r"(curl|wget)[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b", r):
        add("unpinned-tool", "a remote install script is piped into the shell: unpinned and unverified",
            "download a pinned release and check its checksum, or use a pinned setup action or image")
    if GATE_RE.search(r) and SWALLOW_RE.search(r):
        add("hidden-failure", f"`{r[:70]}` ignores its own failure", "remove `|| true`; a gate must fail the job")
    if pl["provider"] != "github":
        for m in SECRET_NAME_RE.finditer(r):
            if re.search(r"\b(echo|printf)\b[^|>\n]*" + re.escape(m.group(0)), r) and not re.search(r"\|\s*\S|>\s*\S", r):
                add("secret-exposure", f"`{m.group(0)}` looks like a secret and is printed to the log",
                    "never print secrets; mask them and pipe them to the tool", "medium")
                break
    return out


def check_repo(pls: list, ctx: Ctx) -> list:
    """What the pipelines of the repository do together: scanning, gates, releases."""
    out = []
    f = ctx.facts
    jobs = _all(pls)
    if not jobs:
        return out
    main = next((pl for pl in pls if pl["triggers"].get("pr") and pl["jobs"]), pls[0])
    corpus = "\n".join(_corpus(j, ctx) for j in jobs) + "\n" + "\n".join(
        " ".join(pl.get("remote_includes", [])) for pl in pls)
    pr_jobs = [j for j in jobs if j["on_pr"]]
    pr_corpus = "\n".join(_corpus(j, ctx) for j in pr_jobs)
    prov = main["provider"]
    # --- scanning
    if not SECRET_SCAN_RE.search(corpus):
        out.append(finding(main, "no-secret-scan", "no secret scanning: a leaked key reaches the shared branch "
                           "unnoticed", "add gitleaks (`gitleaks dir --redact .`) to the pipeline; keep the host's "
                           "own secret scanning and push protection on too", snippet=_scan_snippet(prov, "no-secret-scan")))
    if not SAST_RE.search(corpus):
        out.append(finding(main, "no-sast", "no static analysis for security bugs", "add semgrep "
                           "(`semgrep scan --config p/default --error`) or CodeQL", snippet=_scan_snippet(prov, "no-sast")))
    if f["has_deps"] and not SCA_RE.search(corpus):
        sev = "low" if f["updater"] else None
        out.append(finding(main, "no-sca", "no dependency vulnerability scan" +
                           (f" ({' and '.join(f['updater'])} updates versions, but nothing gates a vulnerable one)"
                            if f["updater"] else ""),
                           "add osv-scanner (`osv-scanner scan source -r .`) or trivy fs", severity=sev,
                           snippet=_scan_snippet(prov, "no-sca")))
    if f["iac"] and not IAC_RE.search(corpus):
        out.append(finding(main, "no-iac-scan", f"infrastructure files ({', '.join(f['iac'][:3])}) are not scanned",
                           "add `trivy config .` (or checkov)", snippet=_scan_snippet(prov, "no-iac-scan")))
    builds_images = bool(f["dockerfiles"]) or bool(DOCKER_BUILD_RE.search(corpus))
    if builds_images and not IMAGE_SCAN_RE.search(corpus):
        out.append(finding(main, "no-container-scan", "container images are built but not scanned",
                           "scan the image before it is pushed: `trivy image --exit-code 1 --severity HIGH,CRITICAL "
                           "<image>`"))
    for pl in pls:
        pcorp = "\n".join(_corpus(j, ctx) for j in pl["jobs"])
        for j in pl["jobs"]:
            d = _direct(j)
            if DOCKER_PUSH_RE.search(d) and (DOCKER_BUILD_RE.search(d) or "docker push" in d) and \
                    not IMAGE_SCAN_RE.search(pcorp):
                out.append(finding(j, "image-push-unscanned", f"job `{j['name']}` pushes an image that nothing scans",
                                   "build, scan with `trivy image --exit-code 1`, then push", line=j["line"],
                                   file=j["file"]))
    # --- quality gates
    if not TEST_RE.search(pr_corpus):
        out.append(finding(main, "no-pr-tests", "no tests run on pull requests" +
                           (" (tests run only after merge or on tags)" if TEST_RE.search(corpus) else ""),
                           "run the test gate on every pull request (merge request)"))
    if pr_jobs and not LINT_RE.search(pr_corpus):
        conf = [x for x in f["linters"] if x != "npm run lint" or "lint" in f["scripts"]]
        out.append(finding(main, "no-lint", "no lint step on pull requests" +
                           (f" although the project configures {', '.join(conf)}" if conf else ""),
                           f"run {conf[0]} in the pipeline" if conf else "add the stack's linter (ruff, eslint or "
                           "biome, golangci-lint, spotless or checkstyle, php-cs-fixer)",
                           severity=None if conf else "low"))
    if pr_jobs and f["typecheckers"] and not TYPECHECK_RE.search(pr_corpus):
        out.append(finding(main, "no-typecheck", f"the project configures {', '.join(f['typecheckers'])}, but no "
                           "pipeline runs it", f"run {f['typecheckers'][0]} on pull requests"))
    if TEST_RE.search(pr_corpus) and not COVERAGE_RE.search(corpus) and not any(j["coverage"] for j in jobs):
        out.append(finding(main, "no-coverage", "test coverage is not measured or reported",
                           "report coverage (pytest --cov, vitest --coverage, JaCoCo, go test -coverprofile) and "
                           "show it on the pull request", snippet="coverage: '/TOTAL.*\\s(\\d+%)/'" if prov == "gitlab"
                           else None))
    gate = [g for g in ((ctx.policy or {}).get("test_gate") or []) if str(g).strip()]
    if gate:
        cmds = [_norm_cmd(x) for x in pr_corpus.splitlines() if x.strip()]
        missing = [g for g in gate if not any(_gate_match(_norm_cmd(g), c) for c in cmds)]
        if missing:
            out.append(finding(main, "test-gate-mismatch", "the delivery policy's test gate is not what CI runs on "
                               f"pull requests: missing `{'`, `'.join(missing[:3])}`",
                               "run the same commands in CI, or update the policy: `dev delivery apply --set "
                               "test_gate=\"…\" --why \"…\"`"))
    # --- path filters
    if f["monorepo"] and pr_jobs and not any(pl["triggers"].get("paths") for pl in pls) and not AFFECTED_RE.search(corpus):
        out.append(finding(main, "no-path-filters", f"a monorepo ({', '.join(f['parts'][:4])}) where every change "
                           "builds every part", {"github": "split workflows with `paths:` filters, or detect the "
                                                           "affected parts (dorny/paths-filter, nx/turbo affected)",
                                                 "gitlab": "use `rules: changes:` per part",
                                                 "bitbucket": "use `condition: changesets: includePaths:`",
                                                 "azure": "use `paths: include:` on triggers"}.get(prov, "")))
    # --- releases and delivery
    out += _check_releases(pls, ctx)
    if any(pl["provider"] == "github" for pl in pls):
        any_uses = any(st.get("uses") and parse_uses(st["uses"]) for j in jobs if j["provider"] == "github"
                       for st in j["steps"])
        if any_uses and not f["dependabot_actions"] and "renovate" not in f["updater"]:
            gh = next(pl for pl in pls if pl["provider"] == "github")
            out.append(finding(gh, "no-actions-updates", "no Dependabot or Renovate updates for GitHub Actions: pinned "
                               "actions stop getting fixes", "add a `github-actions` entry to .github/dependabot.yml "
                               "(weekly, grouped)", file=".github/dependabot.yml",
                               snippet="version: 2\nupdates:\n  - package-ecosystem: github-actions\n    directory: /\n"
                                       "    schedule:\n      interval: weekly\n    groups:\n      actions:\n"
                                       "        patterns: [\"*\"]"))
    return out


_SCAN_TOOL = {"no-secret-scan": "gitleaks", "no-sast": "semgrep", "no-sca": "osv-scanner", "no-iac-scan": "trivy"}
_SCAN_ITEM = {"gitlab": None, "bitbucket": "- step:", "azure": "- script:"}  # how one scanner's block starts


def _scan_snippet(prov: str, finding_id: str) -> str | None:
    """The block of devkit's own security fragment for this host (the one `dev ci init` writes, images pinned by
    digest) that adds the missing scanner: a GitLab job, a Bitbucket step, an Azure script step."""
    tool = _SCAN_TOOL.get(finding_id)
    if prov not in _SCAN_ITEM or not tool:
        return None
    text = textwrap.dedent(render(_tpl(f"{prov}/security.yml"), {}, _pins()))
    if prov == "azure":  # the steps of the one security job
        text = textwrap.dedent(text.split("steps:\n", 1)[1])
    start = _SCAN_ITEM[prov]
    if start is None:
        blocks = [b for b in text.strip().split("\n\n") if b.strip()]
    else:
        blocks, cur = [], []
        for ln in text.splitlines():
            if ln.startswith(start) and cur:
                blocks.append("\n".join(cur)); cur = []
            cur.append(ln)
        blocks.append("\n".join(cur))
    hit = [b.strip("\n") for b in blocks if tool in b]
    if len(hit) != 1:
        return None
    # devkit's pipelines have a lint stage; any other GitLab pipeline has GitLab's default `test` stage
    return hit[0].replace("\n  stage: lint\n", "\n  stage: test\n") if prov == "gitlab" else hit[0]


def _norm_cmd(c: str) -> list:
    c = re.sub(r"(^|\s)(/usr/bin/|/usr/local/bin/|/opt/homebrew/bin/)?python3?(\.\d+)?\b", r"\1python", str(c))
    c = re.sub(r"(^|\s)\./(mvnw|gradlew)\b", lambda m: m.group(1) + m.group(2).replace("w", ""), c)
    c = re.sub(r"(^|\s)(uv run|poetry run|pipenv run|pdm run)\s+", r"\1", c)
    c = re.sub(r"(^|\s)python -m (pytest|mypy|ruff|black|isort|flake8|pylint|coverage|pyright)\b", r"\1\2", c)
    return [t for t in c.split() if t not in ("-B", "-ntp", "--batch-mode", "-q", "--quiet")]


def _gate_match(gate: list, cmd: list) -> bool:
    """Every token of the gate command appears in the CI command, in order."""
    if not gate:
        return True
    k = 0
    for t in cmd:
        if k < len(gate) and t == gate[k]:
            k += 1
    return k == len(gate)


def _sample_tag(fmt: str) -> str:
    fmt = str(fmt or "")
    if "YY" in fmt:
        return fmt.replace("YY.MM.PATCH", "26.11.0")
    return fmt.replace("MAJOR.MINOR.PATCH", "1.2.3") or "v1.2.3"


def _tag_matches(pl: dict, tag: str) -> bool:
    pats = pl["triggers"].get("tags") or []
    prov = pl["provider"]
    for p_ in pats:
        if prov == "github" and gh_glob(p_, tag):
            return True
        if prov in ("bitbucket", "azure") and (_bb_glob(p_, tag) or fnmatch.fnmatch(tag, p_)):
            return True
        if prov == "gitlab":
            m = re.search(r"CI_COMMIT_TAG\s*=~\s*/(.+)/", p_)
            if p_ == "**" or not m:
                return True
            try:
                if re.search(m.group(1), tag):
                    return True
            except re.error:
                return True
    return False


_NOT_RELEASE = re.compile(r"(^|[\W_])(test|tests|testing|staging|preview|dry[- ]?run|canary|nightly|snapshot)($|[\W_])",
                          re.I)


def _publishes(j: dict) -> bool:
    if _NOT_RELEASE.search(f"{j['id']} {j['name']} {j.get('environment') or ''}"):
        return False  # a test registry, a preview or a dry run: not a release
    d = _direct(j)
    if PUBLISH_RE.search(d) or j.get("release_kw"):
        return True
    for st in j["steps"]:
        u = (st.get("uses") or "").lower()
        if u.startswith("docker/build-push-action") and str((st.get("with") or {}).get("push")).lower() == "true":
            return True
    return False


def _needs_closure(j: dict, by_id: dict) -> set:
    seen, stack = set(), list(j["needs"])
    while stack:
        n = stack.pop()
        if n in seen or n not in by_id:
            continue
        seen.add(n)
        stack += by_id[n]["needs"]
    return seen


def _check_releases(pls: list, ctx: Ctx) -> list:
    out = []
    pol = ctx.policy or {}
    fmt = pol.get("tag_format")
    for pl in pls:
        pubs = [j for j in pl["jobs"] if _publishes(j)]
        if not pubs:
            continue
        prov = pl["provider"]
        trig = pl["triggers"]
        called = prov == "github" and trig.get("call") and not (trig.get("pr") or trig.get("push") or trig.get("release"))
        scope = [o for o in pls if o["provider"] == prov] if called else [pl]
        pcorp = "\n".join(_corpus(j, ctx) for o in scope for j in o["jobs"])
        for j in ([] if called else pubs):
            if j["on_pr"]:
                when = "on every branch push (the default pipeline)" if j.get("kind") == "default" else \
                    "on pull requests"
                out.append(finding(j, "release-not-tag-driven", f"job `{j['name']}` publishes {when}",
                                   "publish only from version tags", line=j["line"], file=j["file"], severity="high"))
            elif not j["on_tag"] and j["on_main"] and pl["triggers"].get("release") is not True:
                out.append(finding(j, "release-not-tag-driven", f"job `{j['name']}` publishes on pushes to the "
                                   "branch, not on a version tag", "publish from tags in the policy's format "
                                   f"({fmt or 'vYY.MM.PATCH'}); deploy previews from main if you need them",
                                   line=j["line"], file=j["file"], severity=None if fmt else "low"))
        tagged = [j for j in pubs if j["on_tag"] and not called]
        if fmt and tagged and pl["triggers"].get("tags") and not _tag_matches(pl, _sample_tag(fmt)):
            out.append(finding(pl, "tag-format-mismatch", f"the tag triggers ({', '.join(map(str, pl['triggers']['tags']))}) "
                               f"do not match the policy's format {fmt} (for example {_sample_tag(fmt)})",
                               "make the tag filter match the policy, or change the policy", line=1))
        if not SBOM_RE.search(pcorp):
            out.append(finding(pubs[0], "no-sbom", f"`{pubs[0]['name']}` publishes without an SBOM",
                               "generate one with syft (`syft scan dir:. -o spdx-json=sbom.spdx.json`) and attach it",
                               line=pubs[0]["line"], file=pubs[0]["file"]))
        if not SIGN_RE.search(pcorp):
            how = {"github": "attest it: actions/attest-build-provenance with `id-token: write` and "
                             "`attestations: write`",
                   "gitlab": "sign it keyless with cosign and `id_tokens: SIGSTORE_ID_TOKEN`"}.get(
                prov, "sign it with cosign (a key in a secured variable)")
            out.append(finding(pubs[0], "no-signing", f"`{pubs[0]['name']}` publishes artifacts that are neither "
                               "signed nor attested", how, line=pubs[0]["line"], file=pubs[0]["file"],
                               severity="low" if prov in ("bitbucket", "azure") else None))
        if not VERIFY_RE.search("\n".join(_direct(j) for o in scope for j in o["jobs"] if j["on_tag"] or j in pubs)):
            out.append(finding(pubs[0], "publish-unverified", f"nothing checks what `{pubs[0]['name']}` published",
                               "download or install the published artifact and check it (checksum, `gh attestation "
                               "verify`, `cosign verify`)", line=pubs[0]["line"], file=pubs[0]["file"]))
        if prov == "github" and pl["triggers"].get("release") and not pl["triggers"].get("tags"):
            continue  # runs after a release was published: the tag's own CI ran the gate
        by_id = {j["id"]: j for j in pl["jobs"]}
        for j in tagged:
            tests_here = [t for t in pl["jobs"] if t["on_tag"] and TEST_RE.search(_corpus(t, ctx))]
            ok_order = False
            if j in tests_here:
                ok_order = True
            elif tests_here:
                if prov == "github":
                    ok_order = bool(_needs_closure(j, by_id) & {t["id"] for t in tests_here})
                elif prov == "gitlab":
                    stages = [str(x) for x in _as_list((pl.get("top") or {}).get("stages"))] or \
                        [".pre", "build", "test", "deploy", ".post"]
                    pos = {s_: k for k, s_ in enumerate(stages)}
                    if j["needs"]:
                        ok_order = bool(_needs_closure(j, by_id) & {t["id"] for t in tests_here})
                    else:
                        ok_order = any(pos.get(t.get("stage"), 0) < pos.get(j.get("stage"), 0) for t in tests_here)
                else:
                    ok_order = True
            if not ok_order:
                elsewhere = any(o is not pl and any(t["on_tag"] and TEST_RE.search(_corpus(t, ctx)) for t in o["jobs"])
                                for o in pls if o["provider"] == prov)
                out.append(finding(j, "release-without-tests",
                                   f"job `{j['name']}` publishes on a tag " +
                                   ("without waiting for the tests" if tests_here or elsewhere else
                                    "and the tag pipeline does not run the tests"),
                                   "make the publish job depend on the test gate (`needs:` the test jobs)",
                                   line=j["line"], file=j["file"], severity="low" if elsewhere and not tests_here
                                   else None))
    return out


# =========================================================================== audit

_run = core.sh      # tests replace these to stay offline
_have = core.have


def _provider_of(rel: str):
    if rel.startswith(".github/workflows/"):
        return "github"
    if rel == ".gitlab-ci.yml":
        return "gitlab"
    if rel.startswith("bitbucket-pipelines."):
        return "bitbucket"
    if "azure-pipelines" in rel or rel.startswith(".azure-pipelines/"):
        return "azure"
    return None


def build(root, provider=None, only=None, overlay=None) -> list:
    """Parse the repository's pipelines (or only the given files; overlay = {path: text} not yet on disk)."""
    root = Path(root)
    overlay = overlay or {}
    found = discover(root)
    for rel in overlay:
        prov = _provider_of(rel)
        if prov and rel not in found.get(prov, []):
            found.setdefault(prov, []).append(rel)
    pls = []
    for prov in PROVIDERS:
        if provider and prov != provider:
            continue
        for rel in found.get(prov, []):
            if only is not None and rel not in only:
                continue
            text = overlay.get(rel, None)
            text = _read(root / rel) if text is None else text
            try:
                tree, err = load_yaml(text), None
            except YamlError as e:
                tree, err = None, e
            if prov == "github":
                pl = build_github(rel, text, tree)
            elif prov == "gitlab":
                pl = build_gitlab(root, rel, text, tree)
            elif prov == "bitbucket":
                pl = build_bitbucket(rel, text, tree)
            else:
                pl = build_azure(root, rel, text, tree)
            pl["parse_error"] = err
            pls.append(pl)
    used = {f for pl in pls if pl["provider"] == "azure" for f in pl["files"][1:]}
    keep = []
    for pl in pls:
        t = pl.get("tree")
        if pl["provider"] == "azure" and (pl["file"] in used or isinstance(t, dict) and "parameters" in t and not any(
                k in t for k in ("trigger", "pr", "schedules", "pool", "resources", "name", "extends"))):
            continue  # a template that another pipeline uses (or only a template): audited through its user
        keep.append(pl)
    return keep


def audit(root, provider=None, policy=None, external=True, visibility=None, only=None, overlay=None) -> dict:
    """Every finding for the repository's pipelines, sorted by severity."""
    root = Path(root)
    ctx = Ctx(root, policy, visibility)
    if overlay:
        for rel, text in overlay.items():
            ctx.lines[rel] = text.splitlines()
            if rel in (".github/dependabot.yml", ".github/dependabot.yaml"):
                ctx.facts["updater"] = sorted(set(ctx.facts["updater"]) | {"dependabot"})
                ctx.facts["dependabot_actions"] = "github-actions" in text
    pls = build(root, provider, only, overlay)
    out, notes = [], []
    if not pls:
        where = PROVIDER_NAMES.get(provider, "any host") if provider else "any host"
        out.append({"provider": provider or "-", "file": "-", "line": 0, "id": "no-pipeline", "category": "quality",
                    "severity": "high", "message": f"no CI pipeline for {where} in this repository",
                    "fix": "dev ci init (detects the stack and the host; --dry-run shows it first)", "autofix": False})
    ok = []
    for pl in pls:
        notes += [f"{pl['file']}: {n}" for n in pl.get("notes", [])]
        if pl.get("parse_error") is not None:
            e = pl["parse_error"]
            out.append(finding(pl, "parse-error", f"cannot read {pl['file']}: {e.msg}", "fix the YAML (the host "
                               "rejects it too)", line=e.line))
            continue
        ok.append(pl)
        out += {"github": check_github, "gitlab": check_gitlab, "bitbucket": check_bitbucket,
                "azure": check_azure}[pl["provider"]](pl, ctx)
        out += check_jobs(pl, ctx)
    out += check_repo(ok, ctx)
    tools = {}
    if external and ok:
        ext, tools = external_linters(root, ok)
        out = merge_external(out, ext)
    seen, uniq = set(), []
    for f in out:
        k = (f["file"], f["line"], f["id"], f["message"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    uniq.sort(key=lambda f: (RANK.get(f["severity"], 9), CATEGORIES.index(f["category"]) if f["category"] in
                             CATEGORIES else 9, f["file"] or "", f["line"]))
    summary = {s_: sum(1 for f in uniq if f["severity"] == s_) for s_ in SEVERITIES}
    summary["fixable"] = sum(1 for f in uniq if f["autofix"])
    providers = {}
    for pl in pls:
        providers.setdefault(pl["provider"], []).extend(x for x in pl["files"] if x not in providers.get(pl["provider"], []))
    return {"root": str(root), "providers": providers, "findings": uniq, "summary": summary, "tools": tools,
            "notes": notes, "policy": bool(policy)}


def exit_code(rep: dict, strict: bool = False) -> int:
    worst = RANK["medium"] if strict else RANK["high"]
    return 1 if any(RANK.get(f["severity"], 9) <= worst for f in rep["findings"]) else 0


# --------------------------------------------------------------------------- external linters

ZIZMOR_MAP = {"unpinned-uses": "action-not-pinned", "template-injection": "script-injection",
              "excessive-permissions": "permissions", "dangerous-triggers": "privileged-checkout",
              "self-hosted-runner": "self-hosted-public", "unpinned-images": "floating-image",
              "hardcoded-container-credentials": "secret-exposure", "unredacted-secrets": "secret-exposure",
              "overprovisioned-secrets": "secret-exposure"}
LINE_SPECIFIC = {"action-not-pinned", "script-injection", "floating-image", "secret-exposure"}
ZIZMOR_SEV = {"informational": "info", "unknown": "low", "low": "low", "medium": "medium", "high": "high"}


def external_linters(root: Path, pls: list):
    """actionlint and zizmor (GitHub), `glab ci lint` (GitLab, when signed in). -> (findings, tool status)."""
    out, tools = [], {}
    gh = [f for pl in pls if pl["provider"] == "github" for f in pl["files"]]
    if gh:
        if _have("actionlint"):
            rc, o, e = _run(["actionlint", "-format", "{{json .}}", "-no-color", *gh], cwd=str(root), timeout=120)
            try:
                items = json.loads(o or "[]") or []
            except ValueError:
                items = []
                tools["actionlint"] = f"error: {(e or o)[:100]}"
            for it in items:
                kind = str(it.get("kind") or "actionlint")
                sev = "high" if kind in ("syntax-check", "expression", "job-needs", "events", "credentials") else \
                    "low" if kind in ("shellcheck", "pyflakes") else "medium"
                out.append({"provider": "github", "file": _rel(root, root / str(it.get("filepath") or gh[0])),
                            "line": int(it.get("line") or 1), "id": f"actionlint/{kind}",
                            "category": "security" if kind == "credentials" else "reliability", "severity": sev,
                            "message": str(it.get("message") or "")[:300], "fix": "see `actionlint` for the rule",
                            "autofix": False, "source": "actionlint"})
            tools.setdefault("actionlint", f"ran: {len(items)} finding(s)")
        else:
            tools["actionlint"] = "not installed (brew install actionlint)"
        if _have("zizmor"):
            rc, o, e = _run(["zizmor", "--offline", "--format", "json", "--min-severity", "low", *gh], cwd=str(root),
                            timeout=180)
            try:
                items = json.loads(o or "[]") or []
            except ValueError:
                items = []
                tools["zizmor"] = f"error: {(e or o)[:100]}"
            n = 0
            for it in items:
                locs = it.get("locations") or [{}]
                prim = next((x for x in locs if (x.get("symbolic") or {}).get("kind") == "Primary"), locs[0])
                key = ((prim.get("symbolic") or {}).get("key") or {}).get("Local") or {}
                path = str(key.get("verbatim_path") or key.get("given_path") or gh[0])
                path = path[2:] if path.startswith("./") else path
                row = (((prim.get("concrete") or {}).get("location") or {}).get("start_point") or {}).get("row")
                ident = str(it.get("ident") or "zizmor")
                det = it.get("determinations") or {}
                ann = (prim.get("symbolic") or {}).get("annotation") or ""
                out.append({"provider": "github", "file": path, "line": int(row) + 1 if row is not None else 1,
                            "id": f"zizmor/{ident}", "category": "security",
                            "severity": ZIZMOR_SEV.get(str(det.get("severity") or "unknown").lower(), "low"),
                            "message": f"{it.get('desc') or ident}" + (f": {ann}" if ann else ""),
                            "fix": f"https://docs.zizmor.sh/audits/#{ident}", "autofix": False, "source": "zizmor"})
                n += 1
            tools.setdefault("zizmor", f"ran: {n} finding(s)")
        else:
            tools["zizmor"] = "not installed (brew install zizmor)"
    if any(pl["provider"] == "gitlab" for pl in pls):
        if not _have("glab"):
            tools["glab"] = "not installed: skipped `glab ci lint`"
        elif _run(["glab", "auth", "status"], cwd=str(root), timeout=20)[0] != 0:
            tools["glab"] = "not signed in: skipped `glab ci lint`"
        else:
            rc, o, e = _run(["glab", "ci", "lint"], cwd=str(root), timeout=60)
            tools["glab"] = "ran: valid" if rc == 0 else "ran: invalid"
            if rc != 0:
                msg = " ".join(x.strip() for x in (o + "\n" + e).splitlines() if x.strip())[:300]
                out.append({"provider": "gitlab", "file": ".gitlab-ci.yml", "line": 1, "id": "glab/lint",
                            "category": "reliability", "severity": "high", "message": msg or "GitLab rejects the file",
                            "fix": "fix what `glab ci lint` reports", "autofix": False, "source": "glab"})
    if any(pl["provider"] == "azure" for pl in pls):
        tools["az"] = "Azure Pipelines has no offline lint: static checks only"
    return out, tools


def merge_external(own: list, ext: list) -> list:
    """Keep devkit's finding where an external tool reports the same thing; note who else saw it."""
    out = list(own)
    for x in ext:
        ident = x["id"].split("/", 1)[1] if "/" in x["id"] else x["id"]
        mapped = ZIZMOR_MAP.get(ident) if x.get("source") == "zizmor" else None
        dup = None
        if mapped:
            for f in own:
                if f["file"] != x["file"]:
                    continue
                if mapped == "permissions" and f["id"] in ("permissions-missing", "permissions-broad"):
                    dup = f
                elif f["id"] == mapped and (mapped not in LINE_SPECIFIC or abs(f["line"] - x["line"]) <= 1):
                    dup = f
                if dup:
                    break
        if dup is not None:
            dup.setdefault("also", [])
            if x["source"] not in dup["also"]:
                dup["also"].append(x["source"])
            continue
        out.append(x)
    return out


# =========================================================================== --fix (GitHub Actions)

def _gh_api(path: str):
    rc, out, err = _run(["gh", "api", path], timeout=20)
    if rc != 0:
        return None, (err or out or "gh api failed")[:200]
    try:
        return json.loads(out or "null"), None
    except ValueError:
        return None, "unreadable answer from GitHub"


def _vkey(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def resolve_pin(owner: str, repo: str, ref: str, api=None):
    """The commit of tag `ref` (annotated tags peeled) and its most precise version tag -> (sha, version, problem)."""
    api = api or _gh_api
    obj, err = api(f"repos/{owner}/{repo}/git/ref/tags/{ref}")
    if obj is None:
        missing = err and re.search(r"404|Not Found", str(err))
        return None, None, (f"{owner}/{repo}@{ref} is not a tag (a branch?): pin it to a release by hand" if missing
                            else f"could not reach GitHub ({str(err)[:80]}): pins skipped")
    if isinstance(obj, list):
        obj = next((o for o in obj if o.get("ref") == f"refs/tags/{ref}"), None) or {}
    typ, sha = (obj.get("object") or {}).get("type"), (obj.get("object") or {}).get("sha")
    for _ in range(5):
        if typ != "tag":
            break
        t, err = api(f"repos/{owner}/{repo}/git/tags/{sha}")
        if t is None:
            return None, None, f"could not peel tag {ref} ({str(err)[:60]})"
        typ, sha = (t.get("object") or {}).get("type"), (t.get("object") or {}).get("sha")
    if typ != "commit" or not sha or not _SHA_RE.match(sha):
        return None, None, f"tag {ref} of {owner}/{repo} does not point at a commit"
    version = ref
    if not re.match(r"^v?\d+\.\d+\.\d+$", ref):
        tags, _ = api(f"repos/{owner}/{repo}/tags?per_page=100")
        cands = [t.get("name") for t in (tags or []) if isinstance(t, dict) and
                 (t.get("commit") or {}).get("sha") == sha and re.match(r"^v?\d+\.\d+\.\d+$", str(t.get("name")))]
        if cands:
            version = max(cands, key=_vkey)
    return sha, version, None


def _insert_point(lines: list, idx: int) -> int:
    """Insert above the comment block that belongs to the line at idx."""
    while idx > 0 and lines[idx - 1].lstrip().startswith("#") and not lines[idx - 1].startswith((" ", "\t")):
        idx -= 1
    return idx


def fix_workflow_text(text: str, findings: list, timeout: int = 30, api=None, cache=None):
    """Apply the safe GitHub autofixes to one workflow's text -> (new text, applied, skipped). Comments stay."""
    applied, skipped = [], []
    try:
        tree = load_yaml(text)
    except YamlError as e:
        return text, applied, [f"cannot read the workflow: {e}"]
    if not isinstance(tree, dict):
        return text, applied, skipped
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl
    ids = {f["id"] for f in findings if f.get("autofix")}
    jobs = tree.get("jobs") if isinstance(tree.get("jobs"), dict) else {}
    jl = _kl(tree, "jobs", 0) if "jobs" in tree else 0
    unit = 2
    if jobs:
        first = _kl(jobs, next(iter(jobs)), 0)
        if first and first - 1 < len(lines):
            unit = max(1, len(lines[first - 1]) - len(lines[first - 1].lstrip(" ")))
    u = " " * unit
    edits = []  # (index, kind, payload): insert lines before index, or replace line at index
    head = []
    if "permissions-missing" in ids and "permissions" not in tree and jl:
        head += [f"permissions:{nl}", f"{u}contents: read # least privilege: a job that needs more asks for it{nl}"]
        applied.append("permissions: contents: read")
    if "no-auto-cancel" in ids and "concurrency" not in tree and jl:
        trig = _gh_triggers(tree.get("on", tree.get(True)))
        only_pr = trig["pr"] and not (trig["push"] or trig["schedule"] or trig["manual"] or trig["release"] or
                                      trig["call"] or trig["privileged"])
        cancel = "true" if only_pr else "${{ github.event_name == 'pull_request' }}"
        if head:
            head.append(nl)
        head += [f"concurrency:{nl}", f"{u}group: ${{{{ github.workflow }}}}-${{{{ github.ref }}}}{nl}",
                 f"{u}cancel-in-progress: {cancel}{' # a newer push replaces a PR run; other runs finish' if not only_pr else ''}{nl}"]
        applied.append(f"concurrency with cancel-in-progress: {cancel}")
    if head:
        at = _insert_point(lines, jl - 1)
        pre = [nl] if at > 0 and lines[at - 1].strip() else []
        edits.append((at, "insert", pre + head + [nl]))
    if "no-job-timeout" in ids:
        want = {f["line"] for f in findings if f["id"] == "no-job-timeout" and f.get("autofix")}
        for jid, j in jobs.items():
            line = _kl(jobs, jid, 0)
            if line not in want or not isinstance(j, dict) or "timeout-minutes" in j or "uses" in j:
                continue
            keys = [_kl(j, k, 0) for k in j]
            if not keys or min(keys) <= line:
                continue
            kind = len(lines[min(keys) - 1]) - len(lines[min(keys) - 1].lstrip(" "))
            at = line  # after the job's key line
            if "runs-on" in j:
                k = _kl(j, "runs-on") - 1
                last, m = k, k + 1
                while m < len(lines):
                    t = lines[m]
                    if not t.strip() or t.lstrip().startswith("#"):
                        m += 1
                        continue
                    if len(t) - len(t.lstrip(" ")) > kind:
                        last = m
                        m += 1
                        continue
                    break
                at = last + 1
            edits.append((at, "insert", [f"{' ' * kind}timeout-minutes: {timeout}{nl}"]))
            applied.append(f"timeout-minutes: {timeout} on job `{jid}`")
    if "action-not-pinned" in ids:
        cache = {} if cache is None else cache
        for f in findings:
            if f["id"] != "action-not-pinned" or not f.get("autofix"):
                continue
            k = f["line"] - 1
            if not 0 <= k < len(lines):
                continue
            m = re.match(r"^(?P<pre>\s*(?:-\s+)?uses:\s*)(?P<q>[\"']?)(?P<name>[^@\s\"']+)@(?P<ref>[^\s\"'#]+)(?P=q)"
                         r"(?P<post>[ \t]*(?:#[^\r\n]*)?)(?P<eol>\r?\n?)$", lines[k])
            if not m:
                skipped.append(f"line {f['line']}: not a simple `uses:` line, left as is")
                continue
            parsed = parse_uses(f"{m.group('name')}@{m.group('ref')}")
            if not parsed or _SHA_RE.match(m.group("ref")):
                continue
            owner, repo, _, ref = parsed
            key = (owner.lower(), repo.lower(), ref)
            if key not in cache:
                cache[key] = resolve_pin(owner, repo, ref, api)
            sha, version, problem = cache[key]
            if not sha:
                skipped.append(f"line {f['line']}: {problem}")
                continue
            old = m.group("post").strip().lstrip("#").strip()
            note = f" · {old}" if old and not re.match(r"^v?\d+(\.\d+)*$", old) else ""
            q = m.group("q")
            new = f"{m.group('pre')}{q}{m.group('name')}@{sha}{q} # {version}{note}{m.group('eol') or nl}"
            edits.append((k, "replace", new))
            applied.append(f"{m.group('name')}@{ref} → {sha[:12]}… # {version}")
    for at, kind, payload in sorted(edits, key=lambda e: (e[0], e[1] == "insert"), reverse=True):
        if kind == "replace":
            lines[at] = payload
        else:
            lines[at:at] = payload
    new = "".join(lines)
    if not text.endswith(("\n", "\r")):
        new = new.rstrip("\r\n")
    if ids & {"full-ci-on-draft", "draft-gate-no-ready"}:  # on the edited text: the lines above have moved
        new, more, left = draft_gate(new, jobs="full-ci-on-draft" in ids)  # else only the types
        applied += more
        skipped += left
    return new, applied, skipped


def apply_fixes(root, rep: dict, policy=None, api=None) -> dict:
    """Edit the GitHub workflows in the working tree. Never commits."""
    root = Path(root)
    res = {"changed": [], "applied": [], "skipped": []}
    by_file = {}
    for f in rep["findings"]:
        if f["provider"] == "github" and f.get("autofix") and f["id"] in AUTOFIX_IDS:
            by_file.setdefault(f["file"], []).append(f)
    cache = {}
    timeout = _default_timeout(policy)
    for rel, fs in sorted(by_file.items()):
        p = root / rel
        text = _read(p)
        new, applied, skipped = fix_workflow_text(text, fs, timeout=timeout, api=api, cache=cache)
        res["applied"] += [f"{rel}: {a}" for a in applied]
        res["skipped"] += [f"{rel}: {s_}" for s_ in skipped]
        if new != text:
            p.write_text(new)
            res["changed"].append(rel)
    return res


# =========================================================================== draft pull requests and branch pushes
#
# Spec P-fy3bwa0pshk. R6: a pull request workflow skips its jobs while the PR is a draft and runs them once when the
# PR is marked ready (`ready_for_review`): `dev ci audit` flags the workflows that run everything on drafts, and
# `dev ci init` and `--fix` write the gate. R1 and R5: `push_ci` reads the CI configuration of the commit a push
# sends, as the hosts do, and says whether that push starts a CI run (True), starts none (False) or devkit cannot
# tell (None). `dev backup` pushes only on False; the guard's push budget counts only the pushes that start CI
# (`push_starts_ci`).

DRAFT_GATE = "github.event.pull_request.draft == false"  # null on a push, and null equals false: pushes still run
PR_TYPES = ["opened", "synchronize", "reopened"]  # GitHub's default activity types for pull_request
READY_TYPES = PR_TYPES + ["ready_for_review"]
_STATUS_FN = re.compile(r"\b(always|success|failure|cancelled)\s*\(")
_DRAFT_CTX = {"github.event_name": "pull_request", "github.event.action": "synchronize",
              "github.event.pull_request.draft": True}


class _Unknown:
    def __repr__(self):
        return "UNKNOWN"


UNKNOWN = _Unknown()  # a value devkit does not know: expressions over it are unknown too (three-valued logic)
_TOKEN = re.compile(r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+(?:\.\d+)?)|(?P<op>==|!=|<=|>=|&&|\|\||[!<>(),])|"
                    r"(?P<id>[A-Za-z_][\w.-]*))")


def _tokens(expr: str):
    out, k = [], 0
    while expr[k:].strip():
        m = _TOKEN.match(expr, k)
        if not m or m.end() == k:
            return None
        k = m.end()
        out.append((m.lastgroup, m.group(m.lastgroup)))
    return out


def _truthy(v):
    if v is UNKNOWN:
        return UNKNOWN
    return not (v is None or v is False or v == "" or (isinstance(v, float) and (v == 0 or v != v)))


def _num(v) -> float:
    if v is None or v is False:
        return 0.0
    if v is True:
        return 1.0
    if isinstance(v, float):
        return v
    s = str(v).strip()
    try:
        return float(s) if s else 0.0
    except ValueError:
        return float("nan")


def _eq(a, b):
    """GitHub's loose equality: strings ignore case, other types meet as numbers (null and false are both 0)."""
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    if type(a) is type(b):
        return a == b
    return _num(a) == _num(b)


def _and(a, b):
    ta, tb = _truthy(a), _truthy(b)
    if ta is False or tb is False:
        return False
    return UNKNOWN if UNKNOWN in (ta, tb) else b


def _or(a, b):
    ta, tb = _truthy(a), _truthy(b)
    if ta is True or tb is True:
        return a if ta is True else b
    return UNKNOWN if UNKNOWN in (ta, tb) else b


def _lookup(ctx: dict, name: str):
    if name in ctx:
        return ctx[name]
    for k, v in ctx.items():  # "github.event.pull_request.": None, the whole object is null on this event
        if k.endswith(".") and (name.startswith(k) or name + "." == k):
            return v
    return UNKNOWN


class _Expr:
    """GitHub's expression syntax (literals, context names, !, comparisons, &&, ||, parentheses and a few functions)
    over the values devkit knows."""

    def __init__(self, toks, ctx, funcs):
        self.t, self.k, self.ctx, self.funcs = toks, 0, ctx, funcs

    def peek(self):
        return self.t[self.k] if self.k < len(self.t) else (None, None)

    def take(self, op=None):
        tok = self.peek()
        if tok == (None, None) or op is not None and tok != ("op", op):
            raise ValueError(op or "end")
        self.k += 1
        return tok

    def parse(self):
        v = self.or_()
        if self.k != len(self.t):
            raise ValueError("trailing tokens")
        return v

    def or_(self):
        v = self.and_()
        while self.peek() == ("op", "||"):
            self.take()
            v = _or(v, self.and_())
        return v

    def and_(self):
        v = self.cmp()
        while self.peek() == ("op", "&&"):
            self.take()
            v = _and(v, self.cmp())
        return v

    def cmp(self):
        v = self.unary()
        kind, op = self.peek()
        if kind == "op" and op in ("==", "!=", "<", ">", "<=", ">="):
            self.take()
            w = self.unary()
            if op not in ("==", "!="):
                return UNKNOWN
            eq = _eq(v, w)
            return eq if eq is UNKNOWN or op == "==" else not eq
        return v

    def unary(self):
        if self.peek() == ("op", "!"):
            self.take()
            v = _truthy(self.unary())
            return v if v is UNKNOWN else not v
        return self.primary()

    def primary(self):
        kind, val = self.take()
        if (kind, val) == ("op", "("):
            v = self.or_()
            self.take(")")
            return v
        if kind == "str":
            return val[1:-1].replace("''", "'")
        if kind == "num":
            return float(val)
        if kind != "id":
            raise ValueError(val)
        if self.peek() == ("op", "("):
            self.take()
            args = []
            if self.peek() != ("op", ")"):
                args.append(self.or_())
                while self.peek() == ("op", ","):
                    self.take()
                    args.append(self.or_())
            self.take(")")
            return self.call(val.lower(), args)
        low = val.lower()
        if low in ("true", "false"):
            return low == "true"
        return None if low == "null" else _lookup(self.ctx, val)

    def call(self, name, args):
        if name in self.funcs:
            return self.funcs[name]
        if name in ("startswith", "endswith", "contains") and len(args) == 2 and all(isinstance(x, str) for x in args):
            a, b = args[0].lower(), args[1].lower()
            return a.startswith(b) if name == "startswith" else a.endswith(b) if name == "endswith" else b in a
        return UNKNOWN


def eval_if(cond, ctx: dict, funcs: dict = None):
    """A job's `if:` under the context values devkit knows -> True, False, or None when the answer depends on a value
    it does not know. `${{ }}` around the whole is optional; text around an expression makes a non-empty string, which
    is always true (zizmor's unsound-condition)."""
    if isinstance(cond, bool):
        return cond
    if cond is None:
        return True
    text = str(cond).strip()
    if not text:
        return True
    m = re.fullmatch(r"\$\{\{(.*)\}\}", text, re.S)
    if m and "${{" not in m.group(1):
        text = m.group(1).strip()
    elif "${{" in text:
        return True
    toks = _tokens(text)
    if not toks:
        return None
    try:
        t = _truthy(_Expr(toks, ctx, dict(funcs or {})).parse())
    except (ValueError, IndexError):
        return None
    return None if t is UNKNOWN else t


def job_results(tree, ctx: dict) -> dict:
    """job id -> True (runs), False (skipped) or None (cannot tell) for one event, whose context values are `ctx`. A
    job whose `if:` calls no status function skips when a job it needs skipped, as on GitHub."""
    jobs = tree.get("jobs") if isinstance(tree, dict) and isinstance(tree.get("jobs"), dict) else {}
    res: dict = {}

    def visit(jid, path):
        if jid in res:
            return res[jid]
        j = jobs.get(jid)
        if not isinstance(j, dict) or jid in path:
            return None
        needs = [visit(str(n), path | {jid}) for n in _as_list(j.get("needs")) if str(n) in jobs]
        skipped, unsure = any(r is False for r in needs), any(r is None for r in needs)
        ran = [r for r in needs if r is not False]
        cond = j.get("if")
        if not _STATUS_FN.search(str(cond if cond is not None else "")):
            v = False if skipped else eval_if(cond, ctx)
            r = None if v is True and unsure else v
        else:
            r = eval_if(cond, ctx, {"always": True, "success": UNKNOWN if unsure else not skipped,
                                    "failure": UNKNOWN if ran else False, "cancelled": UNKNOWN if ran else False})
        res[jid] = r
        return r

    for jid in jobs:
        visit(str(jid), frozenset())
    return res


def _verdict(results):
    """Several jobs' results -> True (one runs), False (none runs) or None."""
    vals = list(results.values()) if isinstance(results, dict) else list(results)
    if any(v is True for v in vals):
        return True
    return False if all(v is False for v in vals) else None


def _gh_events(on) -> dict:
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {str(e): None for e in on}
    return dict(on) if isinstance(on, dict) else {}


def _pr_types(cfg) -> list:
    return [str(x) for x in _as_list(cfg.get("types"))] if isinstance(cfg, dict) else []


def _gh_draft_checks(pl: dict) -> list:
    """`dev ci audit`: a pull request workflow that runs every job on drafts, or that skips drafts but starts nothing
    when a draft is marked ready (R6)."""
    tree = pl.get("tree")
    on = tree.get("on", tree.get(True)) if isinstance(tree, dict) else None
    events = _gh_events(on)
    if "pull_request" not in events:
        return []
    types = _pr_types(events["pull_request"])
    if types and not any(t in PR_TYPES for t in types):
        return []  # it never runs on a draft's pushes
    res = job_results(tree, _DRAFT_CTX)
    if not res:
        return []
    line = _kl(on, "pull_request", _kl(tree, "on", 1)) if isinstance(on, dict) else _kl(tree, "on", 1)
    snippet = "pull_request:\n  types: [" + ", ".join(READY_TYPES) + "]"
    if any(v is False for v in res.values()):
        if "ready_for_review" in types:
            return []
        return [finding(pl, "draft-gate-no-ready", "jobs skip draft pull requests, but `ready_for_review` is not "
                        "in `on.pull_request.types`: marking a draft ready starts no run until the next push",
                        "add `types: [opened, synchronize, reopened, ready_for_review]` to `pull_request:`",
                        line=line, autofix=True, snippet=snippet)]
    return [finding(pl, "full-ci-on-draft", f"every job runs on draft pull requests: each push to a draft runs "
                    f"all {len(res)} job(s) of {Path(pl['file']).name}",
                    f"gate the jobs with `if: {DRAFT_GATE}` and add `ready_for_review` to `types`, so the one run "
                    "starts when the PR is marked ready (dev ci audit --fix writes both)", line=line, autofix=True,
                    snippet=snippet + f"\n...\n    if: {DRAFT_GATE}")]


def _job_if_at(lines, j, ind):
    """Where a new `if:` goes in job mapping `j`: after a one-line `name:` that comes first, else before the first
    key -> (line index, indentation)."""
    keys = sorted(j, key=lambda k: _kl(j, k, 0))
    first = _kl(j, keys[0], 0) - 1
    if keys[0] == "name" and isinstance(j.get("name"), str) and (len(keys) == 1 or _kl(j, keys[1], 0) - 1 == first + 1):
        return first + 1, ind(first)
    return first, ind(first)


def draft_gate(text: str, jobs: bool = True):
    """Gate a GitHub workflow on draft pull requests -> (new text, applied, skipped). `on.pull_request.types` gains
    `ready_for_review`, so marking a draft ready starts the one run; with `jobs`, every job that would run on a draft
    and needs no other job gets `if: github.event.pull_request.draft == false` (the jobs that need it skip with it).
    Comments and layout stay; nothing changes when the workflow has no pull request trigger."""
    applied, skipped = [], []
    try:
        tree = load_yaml(text)
    except YamlError as e:
        return text, applied, [f"cannot read the workflow: {e}"]
    if not isinstance(tree, dict):
        return text, applied, skipped
    on = tree.get("on", tree.get(True))
    if not isinstance(on, dict) or "pull_request" not in on:
        if "pull_request" in _gh_events(on):
            skipped.append("`on:` is not a mapping: write it as one (`on:`, then `pull_request:`) for the draft gate")
        return text, applied, skipped
    cfg = on.get("pull_request")
    types = _pr_types(cfg)
    if types and not any(t in PR_TYPES for t in types):
        return text, applied, skipped
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl

    def ind(k):
        return len(lines[k]) - len(lines[k].lstrip(" "))

    edits = []  # (index, kind, payload): insert before index, or replace the line at index
    pk = _kl(on, "pull_request", 0) - 1
    if "ready_for_review" not in types and pk >= 0:
        unit = max(1, ind(pk) - ind(_kl(tree, "on", 1) - 1))
        flow = "types: [" + ", ".join(types + [t for t in READY_TYPES if t not in types]) + "]"
        if not isinstance(cfg, dict) or not cfg:
            m = re.match(r"^(?P<pre>\s*pull_request:)\s*(?:\{\s*\}|~|null)?\s*(?P<post>#[^\r\n]*)?\r?\n$", lines[pk])
            if m:
                edits.append((pk, "replace", m.group("pre") + (" " + m.group("post") if m.group("post") else "") + nl))
                edits.append((pk + 1, "insert", [" " * (ind(pk) + unit) + flow + nl]))
            else:
                skipped.append(f"line {pk + 1}: `pull_request:` is not a plain key; add `{flow}` by hand")
        elif "types" not in cfg:
            first = min(_kl(cfg, k, 0) for k in cfg) - 1
            edits.append((first, "insert", [" " * ind(first) + flow + nl]))
        else:
            tk = _kl(cfg, "types", 0) - 1
            seq = cfg.get("types")
            m = re.match(r"^(?P<pre>\s*types:\s*)\[(?P<items>[^\]#]*)\](?P<post>[^\r\n]*)\r?\n$", lines[tk])
            if m:
                items = m.group("items").strip().rstrip(",")
                edits.append((tk, "replace", f"{m.group('pre')}[{items}, ready_for_review]{m.group('post')}{nl}"))
            elif isinstance(seq, YSeq) and seq.ilines:
                last = seq.ilines[-1] - 1
                dash = re.match(r"^(\s*-\s*)", lines[last])
                edits.append((last + 1, "insert", [(dash.group(1) if dash else " " * ind(last) + "- ")
                                                   + "ready_for_review" + nl]))
            else:
                skipped.append(f"line {tk + 1}: add `ready_for_review` to `types` by hand")
        if not skipped:
            applied.append("`ready_for_review` in on.pull_request.types")
    if jobs:
        res = job_results(tree, _DRAFT_CTX)
        all_jobs = tree.get("jobs") if isinstance(tree.get("jobs"), dict) else {}
        for jid, j in all_jobs.items():
            if not isinstance(j, dict) or not j or res.get(str(jid)) is False:
                continue
            cond = j.get("if")
            if j.get("needs") and not _STATUS_FN.search(str(cond or "")):
                continue  # it skips with the job it needs
            if cond is None:
                at, sp = _job_if_at(lines, j, ind)
                edits.append((at, "insert", [" " * sp + f"if: {DRAFT_GATE} # drafts wait for ready_for_review" + nl]))
            else:
                ck = _kl(j, "if", 0) - 1
                expr = str(cond).strip()
                m = re.fullmatch(r"\$\{\{(.*)\}\}", expr, re.S)
                expr = m.group(1).strip() if m and "${{" not in m.group(1) else expr
                raw = lines[ck].split("if:", 1)[1].strip() if "if:" in lines[ck] else ""
                if not raw or raw[:1] in "|>" or "${{" in expr or ": " in expr or " #" in expr or "\n" in expr:
                    skipped.append(f"job `{jid}`: add `&& {DRAFT_GATE}` to its `if:` by hand")
                    continue
                edits.append((ck, "replace", " " * ind(ck) + f"if: ${{{{ ({expr}) && {DRAFT_GATE} }}}}{nl}"))
            applied.append(f"`if: {DRAFT_GATE}` on job `{jid}`")
    for at, kind, payload in sorted(edits, key=lambda e: (e[0], e[1] == "insert"), reverse=True):
        if kind == "replace":
            lines[at] = payload
        else:
            lines[at:at] = payload
    new = "".join(lines)
    if not text.endswith(("\n", "\r")):
        new = new.rstrip("\r\n")
    return new, applied, skipped


# --------------------------------------------------------------------------- does a push start CI (R1, R5)

UNREAD_CI = (  # CI configuration devkit cannot read: a push may start it, so devkit cannot tell (R1 does not push)
    (".circleci/", "CircleCI"), (".travis.yml", "Travis CI"), ("Jenkinsfile", "Jenkins"), (".drone.yml", "Drone"),
    ("appveyor.yml", "AppVeyor"), (".appveyor.yml", "AppVeyor"), (".buildkite/", "Buildkite"),
    (".woodpecker.yml", "Woodpecker"), (".woodpecker/", "Woodpecker"), (".gitea/workflows/", "Gitea Actions"),
    (".forgejo/workflows/", "Forgejo Actions"), (".semaphore/", "Semaphore"), ("cloudbuild.yaml", "Cloud Build"),
    ("cloudbuild.yml", "Cloud Build"), ("buildspec.yml", "AWS CodeBuild"), ("bitrise.yml", "Bitrise"),
    ("codemagic.yaml", "Codemagic"), ("netlify.toml", "Netlify"), ("vercel.json", "Vercel"))
MAX_PATHS = 300  # GitHub reads the first 300 changed files of a push for its paths filters
_YAML_EXT = (".yml", ".yaml")


def _ci_files(names: list) -> list:
    """The files of a commit that hold its CI configuration (every YAML file too when a GitLab or Azure pipeline may
    include or extend one), as relative paths."""
    out = [n for n in names if n.startswith(".github/workflows/") and n.count("/") == 2 and n.endswith(_YAML_EXT)]
    out += [n for n in names if n in (".gitlab-ci.yml", "bitbucket-pipelines.yml", "bitbucket-pipelines.yaml")]
    azure = [n for n in names if re.fullmatch(r"([^/]*\.)?azure-pipelines\.ya?ml", n) or n.startswith(
        ".azure-pipelines/") and n.endswith(_YAML_EXT)]
    out += azure
    if ".gitlab-ci.yml" in names or azure:
        out += [n for n in names if n.endswith(_YAML_EXT) and n not in out and not any(
            part in SKIP_DIRS for part in n.split("/")[:-1])][:2000]
    return out


def _materialize(top: str, sha: str, files: list, dest: Path) -> bool:
    """Write `files` as they are in commit `sha` under `dest` (git archive; nothing lands outside `dest`)."""
    import io
    import subprocess
    import tarfile
    if not files:
        return True
    try:
        p = subprocess.run(["git", "-C", top, "archive", "--format=tar", sha, "--", *files], capture_output=True,
                           timeout=60, env=core.GIT_ENV)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if p.returncode != 0:
        return False
    try:
        with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tf:
            for m in tf.getmembers():
                parts = Path(m.name).parts
                if not m.isfile() or m.name.startswith("/") or ".." in parts or m.size > 2_000_000:
                    continue
                target = dest.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                fh = tf.extractfile(m)
                target.write_bytes(fh.read() if fh else b"")
    except (tarfile.TarError, OSError):
        return False
    return True


def _changed_files(top: str, sha: str, remote_sha, base):
    """A function that lists the files a push of `sha` changes, as a paths filter sees them (None: unknown). A new
    branch gets every file its commits change, against the base branch and against the remote: more files make a run
    more likely, which is the safe side for a backup."""
    cache = {}

    def get():
        if "v" in cache:
            return cache["v"]
        if remote_sha:
            rc, out, _ = core.git(top, "diff", "--name-only", "-z", remote_sha, sha, timeout=60)
            cache["v"] = sorted(x for x in out.split("\0") if x) if rc == 0 else None
            return cache["v"]
        files = set()
        steps = [["log", "--format=", "--name-only", "-z", sha, "--not", "--remotes"],
                 ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--root", sha]]
        mb = core.gitout(top, "merge-base", base, sha) if base else ""
        if mb:
            steps.append(["diff", "--name-only", "-z", mb, sha])
        for args in steps:
            rc, out, _ = core.git(top, *args, timeout=60)
            if rc != 0:
                cache["v"] = None
                return None
            files |= {x.strip("\n") for x in out.split("\0") if x.strip("\n")}
        cache["v"] = sorted(files)
        return cache["v"]

    return get


def _gh_filter(patterns, value: str) -> bool:
    """GitHub's ordered filter list: the last pattern that matches decides, and `!` excludes."""
    hit = False
    for p in [str(x) for x in _as_list(patterns)]:
        if p.startswith("!"):
            if gh_glob(p[1:], value):
                hit = False
        elif gh_glob(p, value):
            hit = True
    return hit


def _gh_push(cfg, branch: str, changed):
    """Does a push of `branch` match this workflow's `push` trigger? -> (True | False | None, why)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    if "branches" in cfg:
        if not _gh_filter(cfg.get("branches"), branch):
            return False, ""
        why = "runs on pushes to " + ", ".join(str(x) for x in _as_list(cfg.get("branches")))
    elif "branches-ignore" in cfg:
        if _gh_filter(cfg.get("branches-ignore"), branch):
            return False, ""
        why = "runs on every push outside its branches-ignore list"
    elif "tags" in cfg or "tags-ignore" in cfg:
        return False, ""  # a tag filter alone: only tag pushes run it
    else:
        why = "runs on every push"
    if "paths" in cfg or "paths-ignore" in cfg:
        files = changed()
        if files is None:
            return None, "filters on paths, and devkit could not list the files the push changes"
        if len(files) > MAX_PATHS:
            return None, f"filters on paths, and the push changes more than {MAX_PATHS} files"
        hit = any(_gh_filter(cfg["paths"], f) for f in files) if "paths" in cfg else \
            any(not _gh_filter(cfg["paths-ignore"], f) for f in files)
        if not hit:
            return False, ""
        why += " that changes its paths"
    return True, why


def _gh_words(events: dict) -> str:
    """What a workflow runs on, in words: "pull requests and pushes to main"."""
    words = []
    for ev, cfg in events.items():
        cfg = cfg if isinstance(cfg, dict) else {}
        if ev == "push":
            if "branches" in cfg:
                words.append("pushes to " + ", ".join(str(x) for x in _as_list(cfg.get("branches"))))
            elif "branches-ignore" in cfg:
                words.append("pushes outside branches-ignore")
            else:
                words.append("tags" if "tags" in cfg or "tags-ignore" in cfg else "pushes")
        elif ev in ("pull_request", "pull_request_target"):
            words.append("pull requests")
        else:
            words.append({"schedule": "a schedule", "workflow_dispatch": "manual runs", "workflow_call": "calls",
                          "release": "releases", "merge_group": "the merge queue"}.get(ev, ev.replace("_", " ")))
    words = list(dict.fromkeys(words))
    return " and ".join([", ".join(words[:-1]), words[-1]] if len(words) > 1 else words) or "nothing"


def _push_ctx(branch: str) -> dict:
    return {"github.event_name": "push", "github.ref": f"refs/heads/{branch}", "github.ref_name": branch,
            "github.ref_type": "branch", "github.head_ref": "", "github.base_ref": "", "github.event.pull_request.": None}


def _pr_ctx(event: str, branch: str, pr: dict) -> dict:
    return {"github.event_name": event, "github.event.action": "synchronize", "github.head_ref": branch,
            "github.event.pull_request.draft": bool(pr.get("draft"))}


def _gh_push_ci(name, tree, branch, changed, pr, prs_known, starts, unsure, quiet):
    events = _gh_events(tree.get("on", tree.get(True)))
    seen = len(starts) + len(unsure) + len(quiet)
    if "push" in events:
        runs, why = _gh_push(events["push"], branch, changed)
        if runs is None:
            unsure.append(f"{name} {why}")
        elif runs:
            jobs = _verdict(job_results(tree, _push_ctx(branch)))
            if jobs is False:
                quiet.append(f"{name} skips every job on a push to {branch}")
            elif jobs is None:
                unsure.append(f"{name} {why}, and devkit cannot evaluate its jobs' `if:`")
            else:
                starts.append((f"{name} {why}", f"add {branch} (or a pattern for it) to `branches-ignore` in {name}"))
    prs = [ev for ev in ("pull_request", "pull_request_target")
           if ev in events and "synchronize" in (_pr_types(events[ev]) or PR_TYPES)]
    if prs and not prs_known:
        unsure.append(f"{name} runs on pull requests, and devkit could not check whether {branch} has one open")
    elif prs and pr:
        label = f"{'draft ' if pr.get('draft') else ''}PR #{pr.get('number')}"
        for ev in prs:
            jobs = _verdict(job_results(tree, _pr_ctx(ev, branch, pr)))
            if jobs is False:
                quiet.append(f"{name} skips every job on {label}")
            elif jobs is None:
                unsure.append(f"{name} runs on {label}, and devkit cannot evaluate its jobs' `if:`")
            else:
                starts.append((f"{name} runs on {label}", "gate its jobs on drafts (dev ci audit --fix) and keep the "
                               "PR a draft until it is ready" if pr.get("draft") else
                               "every push to a PR that is ready for review runs its CI"))
    if len(starts) + len(unsure) + len(quiet) == seen:
        quiet.append(f"{name} runs on {_gh_words(events)}")


def _azure_push_ci(name, tree, branch, pr, prs_known, starts, unsure, quiet):
    def strip(p):
        p = str(p)
        return p[len("refs/heads/"):] if p.startswith("refs/heads/") else p

    t = tree.get("trigger", "__absent__")
    runs, why = False, ""
    if t == "__absent__":
        runs, why = True, "runs on every push (no `trigger:` means every branch)"
    elif isinstance(t, (list, str)) and t != "none":
        runs = any(fnmatch.fnmatchcase(branch, strip(p)) for p in _as_list(t))
        why = "runs on pushes to " + ", ".join(str(x) for x in _as_list(t))
    elif isinstance(t, dict):
        br = t.get("branches")
        inc = _as_list(br.get("include") if isinstance(br, dict) else br) or ["*"]
        exc = _as_list(br.get("exclude")) if isinstance(br, dict) else []
        runs = any(fnmatch.fnmatchcase(branch, strip(p)) for p in inc) and not any(_bb_glob(strip(p), branch)
                                                                                    for p in exc)
        why = "runs on pushes to " + ", ".join(str(x) for x in inc)
        if runs and t.get("paths"):
            unsure.append(f"{name} filters on paths, which devkit does not evaluate for Azure Pipelines")
            return
    if runs:
        starts.append((f"{name} {why}", "set `trigger: branches: exclude:` for the branch"))
    elif tree.get("pr", "__absent__") not in ("none", None) and not (prs_known and not pr):
        unsure.append(f"{name} may run pull request builds, and devkit cannot check for an open pull request on "
                      f"{branch}")
    else:
        quiet.append(f"{name} runs no pipeline for a push to {branch}")


def push_ci(top, branch: str, sha: str = None, remote_sha: str = None, base: str = None, pr=None,
            prs_known: bool = True) -> dict:
    """Would pushing commit `sha` (default: the local branch) to `branch` on the remote start a CI run? It reads the
    CI configuration of that commit, as the hosts do.

    -> {"starts": True | False | None, "why": words, "fix": what to change so it does not, "files": [...]}. None: devkit
    cannot tell (a CI it cannot read, a paths filter it cannot evaluate, a pull request it could not look up).
    remote_sha: the remote branch's commit, None for a new branch (for paths filters). base: the default branch ref.
    pr: the branch's open pull request {"number", "draft"} or None; prs_known: False when devkit could not look."""
    top = str(top)
    sha = sha or core.gitout(top, "rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}")
    if not sha:
        return {"starts": None, "why": f"{branch} has no commit to read the CI configuration from", "fix": "",
                "files": []}
    rc, out, _ = core.git(top, "ls-tree", "-r", "-z", "--name-only", sha, timeout=60)
    if rc != 0:
        return {"starts": None, "why": f"devkit could not list the files of {sha[:7]}", "fix": "", "files": []}
    names = [n for n in out.split("\0") if n]
    for n in names:
        for entry, host in UNREAD_CI:
            if n == entry or entry.endswith("/") and n.startswith(entry):
                return {"starts": None, "why": f"{n} configures {host}, which devkit cannot read", "fix": "",
                        "files": [n]}
    import tempfile
    with tempfile.TemporaryDirectory(prefix="devkit-ci-") as tmp:
        if not _materialize(top, sha, _ci_files(names), Path(tmp)):
            return {"starts": None, "why": f"devkit could not read the CI files of {sha[:7]}", "fix": "", "files": []}
        pls = build(tmp)
    files = [pl["file"] for pl in pls]
    if not pls:
        return {"starts": False, "why": "the commit has no CI configuration", "fix": "", "files": []}
    changed = _changed_files(top, sha, remote_sha, base)
    starts, unsure, quiet = [], [], []
    for pl in pls:
        tree = pl.get("tree")
        name = Path(pl["file"]).name if pl["provider"] == "github" else pl["file"]
        if pl.get("parse_error") is not None or not isinstance(tree, dict):
            unsure.append(f"devkit cannot read {pl['file']}")
        elif pl["provider"] == "github":
            _gh_push_ci(name, tree, branch, changed, pr, prs_known, starts, unsure, quiet)
        elif pl["provider"] == "gitlab":
            kinds = set()
            for j in pl["jobs"]:
                eff = j.get("map") or {}
                kinds |= _gl_rule_kinds(eff.get("rules"), eff.get("only"), eff.get("except")) & set(
                    pl.get("workflow_kinds") or ())
            if "branch" in kinds:
                starts.append((f"{name} runs a branch pipeline on every push", "add a `workflow: rules:` entry with "
                               "`when: never` for branch pipelines outside the default branch"))
            elif "mr" in kinds and not (prs_known and not pr):
                unsure.append(f"{name} runs merge request pipelines, and devkit cannot check for an open merge request "
                              f"on {branch}")
            else:
                quiet.append(f"{name} runs no pipeline for a push to {branch}")
        elif pl["provider"] == "bitbucket":
            section = tree.get("pipelines") if isinstance(tree.get("pipelines"), dict) else {}
            br = section.get("branches") if isinstance(section.get("branches"), dict) else {}
            hit = [str(p) for p in br if _bb_glob(str(p), branch)]
            if "default" in section or hit:
                starts.append((f"{name} runs `{('branches: ' + hit[0]) if hit else 'default'}` on a push to {branch}",
                               "remove `default:` or narrow `branches:`"))
            elif "pull-requests" in section and not (prs_known and not pr):
                unsure.append(f"{name} runs pull request pipelines, and devkit cannot check for an open pull request "
                              f"on {branch}")
            else:
                quiet.append(f"{name} runs no pipeline for a push to {branch}")
        else:
            _azure_push_ci(name, tree, branch, pr, prs_known, starts, unsure, quiet)
    if starts:
        return {"starts": True, "why": "; ".join(w for w, _ in starts), "fix": starts[0][1], "files": files}
    if unsure:
        return {"starts": None, "why": "; ".join(unsure), "fix": "", "files": files}
    return {"starts": False, "why": "; ".join(quiet), "fix": "", "files": files}


def open_pr(pr_list, branch: str):
    """{"number", "draft", "url"} of the branch's open pull request in a `gh pr list` result, or None."""
    for p in pr_list or []:
        if isinstance(p, dict) and p.get("headRefName") == branch and str(p.get("state") or "OPEN").upper() == "OPEN":
            return {"number": p.get("number"), "draft": bool(p.get("isDraft", p.get("draft"))), "url": p.get("url")}
    return None


def cached_pr(top, branch: str, max_age: int = 900):
    """The branch's open pull request from the registry's cache when it is at most `max_age` seconds old -> (pr or
    None, known). Only GitHub remotes have one; known is False without a fresh cache."""
    import time
    from . import registry
    remote = core.gitout(top, "config", "--get", "remote.origin.url")
    parsed = core.parse_remote(remote) if remote else None
    if not parsed or parsed[0] != "github.com" or not parsed[1]:
        return None, False
    f = Path(registry.STATE) / "prs" / f"{parsed[1]}__{parsed[2]}.json"
    try:
        if time.time() - f.stat().st_mtime > max_age:
            return None, False
        data = json.loads(f.read_text())
    except (OSError, ValueError):
        return None, False
    return open_pr(data, branch), True


def push_starts_ci(top, branch: str, src: str = None, remote: str = "origin") -> bool:
    """For the guard's push budget (R5), which counts only the pushes that start CI: False only when devkit can tell
    from the CI configuration that pushing `src` (default: the local branch) to `branch` starts no run, with the pull
    requests of a registry cache at most 15 minutes old. Never raises."""
    try:
        top = str(top)
        sha = core.gitout(top, "rev-parse", "--verify", "-q", f"{src or 'refs/heads/' + branch}^{{commit}}")
        if not sha:
            return True
        rsha = core.gitout(top, "rev-parse", "--verify", "-q", f"refs/remotes/{remote}/{branch}^{{commit}}")
        pr, known = cached_pr(top, branch)
        return push_ci(top, branch, sha, remote_sha=rsha or None, pr=pr, prs_known=known)["starts"] is not False
    except Exception:
        return True


# =========================================================================== dev ci init

def _pins() -> dict:
    return json.loads((TEMPLATES / "pins.json").read_text())


def _tpl(name: str) -> str:
    return (TEMPLATES / name).read_text()


_SEC_BLOCK = re.compile(r"^[ \t]*@\{([#^])([\w-]+)\}[ \t]*\n(.*?)^[ \t]*@\{/\2\}[ \t]*\n", re.M | re.S)
_SEC_INLINE = re.compile(r"@\{([#^])([\w-]+)\}(.*?)@\{/\2\}", re.S)
_VAR = re.compile(r"@\{(?:(uses|image|tool|bin_url|bin_sha):)?([\w./-]+)\}")


def render(text: str, ctx: dict, pins: dict) -> str:
    """A tiny template language: @{name}, @{#flag}…@{/flag}, @{^flag}…@{/flag}, @{uses:owner/repo},
    @{image:key}, @{tool:key}, @{bin_url:key}, @{bin_sha:key}. Multi-line values keep the placeholder's indent."""
    def sec(m):
        val = ctx.get(m.group(2))
        show = bool(val) if m.group(1) == "#" else not val
        return render_sections(m.group(3)) if show else ""

    def render_sections(t):
        prev = None
        while prev != t:
            prev = t
            t = _SEC_BLOCK.sub(sec, t)
            t = _SEC_INLINE.sub(sec, t)
        return t

    def value(kind, name):
        if kind == "uses":
            key = "/".join(name.split("/")[:2])
            a = pins["actions"][key]
            return f"{name}@{a['sha']} # {a['version']}"
        if kind == "image":
            im = pins["images"][name]
            return f"{im['name']}:{im['version']}" + (f"@{im['digest']}" if im.get("digest") else "")
        if kind == "tool":
            return str(pins["tools"][name])
        if kind == "bin_url":
            return pins["binaries"][name]["url"]
        if kind == "bin_sha":
            return pins["binaries"][name]["sha256"]
        if name not in ctx:
            raise KeyError(f"template variable @{{{name}}} has no value")
        v = ctx[name]
        return "\n".join(v) if isinstance(v, list) else str(v)

    text = render_sections(text)
    out = []
    for line in text.split("\n"):
        def sub(m, line=line):
            v = value(m.group(1), m.group(2))
            if "\n" in v:
                pre = line[:m.start()]
                ind = pre if not pre.strip() else " " * (len(pre) - len(pre.lstrip()))
                v = v.replace("\n", "\n" + ind)
            return v
        out.append(_VAR.sub(sub, line))
    text = "\n".join(x.rstrip() for x in "\n".join(out).split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n") + "\n"


def _yq(v: str) -> str:
    """A scalar as YAML: plain when that reads back the same, else double-quoted."""
    v = str(v)
    if re.search(r":(\s|$)|\s#|^[\[\]{}&*!|>'\"%@`,?#-]|^\s|\s$", v) or v.lower() in ("true", "false", "null", "~",
                                                                                      "yes", "no", "on", "off"):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return v


def _yaml_item(cmd: str) -> str:
    """A command as a YAML sequence item, quoted or as a block when plain YAML would misread it."""
    if "\n" in cmd:
        return "- |\n  " + cmd.replace("\n", "\n  ")
    risky = re.search(r":\s|\s#|^[\[\]{}&*!|>'\"%@`,?-]|:$", cmd)
    if not risky:
        return f"- {cmd}"
    if "'" not in cmd:
        return f"- '{cmd}'"
    return "- |\n  " + cmd


def _has(text: str, *names) -> bool:
    return any(re.search(r"(^|[\"'\s\[,])" + re.escape(n) + r"([\"'\s=<>~!\[\],;]|$)", text, re.M) for n in names)


def _go_version(f) -> str:
    m = re.search(r"(?m)^go\s+(\d+\.\d+)", f["text"].get("go.mod", ""))
    return m.group(1) if m else None


def _java_version(f) -> str:
    t = f["text"].get("pom.xml", "") + f["text"].get("build.gradle", "") + f["text"].get("build.gradle.kts", "")
    for rx in (r"<java\.version>\s*(\d+)", r"<maven\.compiler\.release>\s*(\d+)", r"<release>\s*(\d+)</release>",
               r"JavaLanguageVersion\.of\((\d+)\)", r"sourceCompatibility\s*=\s*['\"]?(?:JavaVersion\.VERSION_)?(\d+)"):
        m = re.search(rx, t)
        if m:
            return m.group(1)
    return None


def _python_version(root: Path, f) -> tuple:
    pv = _read(root / ".python-version").strip().splitlines()
    if pv and pv[0].strip():
        return pv[0].strip(), True
    m = re.search(r"requires-python\s*=\s*[\"'][^\"']*?>=\s*(3\.\d+)", f["text"].get("pyproject.toml", ""))
    default = _pins()["runtimes"]["python"]
    if m and _vkey(m.group(1)) > _vkey(default):
        return m.group(1), False
    return default, False


def detect_stacks(root, stack: str = "auto", f=None) -> list:
    """The stacks in the repository root and how to lint, test and build each, from its files and lockfiles."""
    root = Path(root)
    f = f or facts(root)
    top = f["top"]
    want = None if stack in (None, "auto") else stack
    out = []
    py = top & {"pyproject.toml", "setup.py", "setup.cfg", "uv.lock", "poetry.lock", "Pipfile"} or \
        any(n.startswith("requirements") and n.endswith(".txt") for n in top)
    py_stdlib = not py and (root / "tests").is_dir() and any((root / "tests").glob("test*.py"))
    if want in (None, "python") and (py or py_stdlib or want == "python"):
        out.append(_stack_python(root, f))
    if want in (None, "node") and ("package.json" in top or want == "node"):
        out.append(_stack_node(root, f))
    if want in (None, "java-maven") and ("pom.xml" in top or want == "java-maven"):
        out.append(_stack_maven(root, f))
    if want in (None, "java-gradle") and (top & {"build.gradle", "build.gradle.kts", "settings.gradle",
                                                  "settings.gradle.kts"} or want == "java-gradle"):
        out.append(_stack_gradle(root, f))
    if want in (None, "go") and ("go.mod" in top or want == "go"):
        out.append(_stack_go(root, f))
    if want in (None, "php") and ("composer.json" in top or want == "php"):
        out.append(_stack_php(root, f))
    if len(out) > 1:
        for s_ in out:
            s_["prefix"] = s_["key"] + "-"
    return out


def _base(key, label, setup, pm) -> dict:
    return {"key": key, "label": label, "setup": setup, "pm": pm, "prefix": "", "install": [], "install_ci": [],
            "lint": [], "typecheck": [], "test": [], "junit": None, "integration": [], "junit_it": None,
            "build": [], "artifact": False, "vars": {}, "rt_image": None, "bb_caches": [], "az_vars": {}}


def _rt_image(pins, key: str, version_tag: str) -> str:
    im = pins["images"][key]
    if version_tag == im["version"]:
        return f"{im['name']}:{im['version']}@{im['digest']}"
    return f"{im['name']}:{version_tag}"


def _stack_python(root: Path, f) -> dict:
    pins = _pins()
    py_text = f["text"].get("pyproject.toml", "") + f["reqs_text"] + _read(root / "requirements-test.txt")
    lock = set(f["lockfiles"])
    pm = "uv" if "uv.lock" in lock else "poetry" if "poetry.lock" in lock else "pip"
    has_manifest = bool(f["top"] & {"pyproject.toml", "setup.py", "setup.cfg"} or f["reqs_text"])
    st = _base("python", "Python", f"python-{pm}", pm)
    version, from_file = _python_version(root, f)
    run = {"uv": "uv run ", "poetry": "poetry run ", "pip": "python -m "}[pm]
    st["vars"] = {"python": version, "python_file": from_file, "pip_cache": pm == "pip" and has_manifest}
    st["rt_image"] = _rt_image(pins, "python", f"{version}-slim")
    if pm == "uv":
        st["install"] = ["uv sync --frozen"]
        st["install_steps"] = ["uv sync --frozen"]
        im = pins["images"]["uv-python"]
        tag = f"{pins['tools']['uv']}-python{version}-trixie-slim"
        st["rt_image"] = f"{im['name']}:{tag}" + (f"@{im['digest']}" if tag == im["version"] else "")
        st["bb_caches"] = ["uv", "pip"]
        st["az_vars"] = {"UV_CACHE_DIR": "$(Pipeline.Workspace)/.cache/uv"}
    elif pm == "poetry":
        st["install"] = ["poetry install --no-interaction"]
        st["install_steps"] = [f"pip install poetry=={pins['tools']['poetry']}", "poetry install --no-interaction"]
        st["bb_caches"] = ["poetry", "pip"]
        st["az_vars"] = {"POETRY_CACHE_DIR": "$(Pipeline.Workspace)/.cache/pypoetry"}
    else:
        inst = []
        if (root / "requirements.txt").is_file():
            inst.append("python -m pip install -r requirements.txt")
        for extra in ("requirements-dev.txt", "requirements-test.txt"):
            if (root / extra).is_file():
                inst.append(f"python -m pip install -r {extra}")
        if f["top"] & {"pyproject.toml", "setup.py"} and re.search(r"\[build-system\]|setup\(", py_text +
                                                                   _read(root / "setup.py")):
            inst.append("python -m pip install -e .")
        st["install"] = inst
        st["install_steps"] = list(inst)
        st["bb_caches"] = ["pip"]
        key = "requirements.txt" if (root / "requirements.txt").is_file() else \
            "pyproject.toml" if "pyproject.toml" in f["top"] else ""
        st["vars"]["pip_key"] = key
        st["az_vars"] = {"PIP_CACHE_DIR": "$(Pipeline.Workspace)/.cache/pip"} if key else {}
    ruff_dep = _has(py_text, "ruff") and pm in ("uv", "poetry")
    rpin = pins["tools"]["ruff"]
    rule = "" if "ruff" in f["linters"] else " --select E9,F63,F7,F82"  # syntax errors and undefined names
    if ruff_dep:
        st["lint"] = [f"{run}ruff check{rule} ."]
    elif pm == "uv":
        st["lint"] = [f"uvx ruff@{rpin} check{rule} ."]
    else:
        st["lint"] = [f"python -m pip install ruff=={rpin}", f"ruff check{rule} ."]
    if _has(py_text, "mypy"):
        st["typecheck"] = [f"{run}mypy ."]
    elif _has(py_text, "pyright", "basedpyright"):
        st["typecheck"] = [f"{run}{'basedpyright' if _has(py_text, 'basedpyright') else 'pyright'}"]
    it_dir = next((d for d in ("tests/integration", "tests/it", "integration_tests") if (root / d).is_dir()), None)
    cov = " --cov --cov-report=xml" if _has(py_text, "pytest-cov") else ""
    if f["test_files"] or (root / "tests").is_dir():
        pytest_dep = _has(py_text, "pytest") or (root / "conftest.py").exists() or "[tool.pytest" in py_text
        ign = f" --ignore={it_dir}" if it_dir else ""
        if pytest_dep:
            pre, cmd = [], f"{run}pytest"
        elif pm == "uv":
            pre, cmd = [], f"uv run --with pytest=={pins['tools']['pytest']} pytest"
        elif pm == "poetry":
            pre, cmd = [f"poetry run python -m pip install pytest=={pins['tools']['pytest']}"], "poetry run pytest"
        else:
            pre, cmd = [f"python -m pip install pytest=={pins['tools']['pytest']}"], "python -m pytest"
        st["test"] = pre + [f"{cmd}{ign}{cov} --junitxml=test-results/junit.xml"]
        st["junit"] = "test-results/junit.xml"
        if it_dir:
            st["integration"] = pre + [f"{cmd} {it_dir} --junitxml=test-results/junit-integration.xml"]
            st["junit_it"] = "test-results/junit-integration.xml"
    if re.search(r"\[build-system\]", py_text) or (root / "setup.py").exists():
        st["build"] = {"uv": ["uv build"], "poetry": ["poetry build"],
                       "pip": ["python -m pip wheel --no-deps -w dist ."]}[pm]
        st["artifact"] = True
    return st


def _stack_node(root: Path, f) -> dict:
    pins = _pins()
    lock = set(f["lockfiles"])
    pm = "pnpm" if "pnpm-lock.yaml" in lock else "yarn" if "yarn.lock" in lock else "npm"
    berry = (root / ".yarnrc.yml").is_file()
    st = _base("node", "Node.js", "node", pm)
    pkg, deps, scripts = f["pkg"], f["node_deps"], f["scripts"]
    run = {"npm": "npm run", "pnpm": "pnpm run", "yarn": "yarn run"}[pm]
    exe = {"npm": "npx", "pnpm": "pnpm exec", "yarn": "yarn exec" if berry else "yarn run"}[pm]
    install = {"npm": "npm ci" if "package-lock.json" in lock or "npm-shrinkwrap.json" in lock else "npm install",
               "pnpm": "pnpm install --frozen-lockfile",
               "yarn": "yarn install --immutable" if berry else "yarn install --frozen-lockfile"}[pm]
    engines = str((pkg.get("engines") or {}).get("node") or "") if isinstance(pkg.get("engines"), dict) else ""
    m = re.search(r"(\d{2})", engines)
    node = m.group(1) if m and int(m.group(1)) >= 18 else pins["runtimes"]["node"]
    node_file = next((n for n in (".nvmrc", ".node-version") if (root / n).is_file()), None)
    pnpm_v = None
    if pm == "pnpm" and not str(pkg.get("packageManager") or "").startswith("pnpm@"):
        pnpm_v = pins["tools"]["pnpm"]
    st["vars"] = {"node": node, "node_file": node_file, "pm": pm, "pnpm": pm == "pnpm", "pnpm_version": pnpm_v,
                  "corepack": pm == "yarn" and berry or (pm == "pnpm"), "lockfile": next(
                      (x for x in ("pnpm-lock.yaml", "yarn.lock", "package-lock.json") if x in lock), "package.json"),
                  "cache_dir": {"npm": ".npm", "pnpm": ".pnpm-store", "yarn": ".yarn/cache"}[pm]}
    if pm == "npm":
        st["install_ci"] = [f"{install} --cache .npm --prefer-offline" if install == "npm ci" else install]
    elif pm == "pnpm":
        st["install_ci"] = ["pnpm config set store-dir .pnpm-store", install]
    else:
        st["install_ci"] = [install]
    st["vars"]["corepack"] = pm in ("pnpm", "yarn") and (berry or pm == "pnpm")
    st["install"] = [install]
    st["install_steps"] = (["corepack enable"] if st["vars"]["corepack"] else []) + st["install_ci"]
    st["rt_image"] = _rt_image(pins, "node", f"{node}-bookworm-slim")
    st["bb_caches"] = [pm]
    st["az_vars"] = {"NODE_CACHE_DIR": "$(Pipeline.Workspace)/.node-cache"}
    if pm == "npm":
        st["az_vars"]["npm_config_cache"] = "$(NODE_CACHE_DIR)"
    if "lint" in scripts:
        st["lint"] = [f"{run} lint"]
    elif "biome" in f["linters"] or "@biomejs/biome" in deps:
        st["lint"] = [f"{exe} biome ci ."]
    elif "eslint" in f["linters"] and "eslint" in deps:
        st["lint"] = [f"{exe} eslint ."]
    tc = next((n for n in ("typecheck", "type-check", "types", "check-types", "tsc") if n in scripts), None)
    if tc:
        st["typecheck"] = [f"{run} {tc}"]
    elif "tsconfig.json" in f["top"] and "typescript" in deps:
        st["typecheck"] = [f"{exe} tsc --noEmit"]
    test = str(scripts.get("test") or "")
    if test and "no test specified" not in test:
        if "vitest" in deps:
            cov = " --coverage" if any(k.startswith("@vitest/coverage-") for k in deps) else ""
            st["test"] = [f"{exe} vitest run{cov} --reporter=default --reporter=junit --outputFile.junit=test-results/junit.xml"]
        elif "jest" in deps and "jest-junit" in deps:
            st["test"] = [f"JEST_JUNIT_OUTPUT_DIR=test-results {exe} jest --ci --coverage --reporters=default --reporters=jest-junit"]
        elif "node --test" in test:
            st["test"] = ["mkdir -p test-results", f"{run} test -- --test-reporter=spec --test-reporter-destination=stdout "
                          "--test-reporter=junit --test-reporter-destination=test-results/junit.xml"]
        else:
            st["test"] = [f"{run} test"]
        st["junit"] = "test-results/junit.xml" if st["test"][-1] != f"{run} test" else None
    it = next((n for n in ("test:integration", "test:it", "integration", "test:e2e") if n in scripts), None)
    if it:
        st["integration"] = [f"{run} {it}"]
        if "vitest" in deps and "vitest" in str(scripts[it]):
            st["integration"] = [f"{run} {it} -- --reporter=default --reporter=junit "
                                 "--outputFile.junit=test-results/junit-integration.xml"]
            st["junit_it"] = "test-results/junit-integration.xml"
    if "build" in scripts:
        st["build"] = [f"{run} build"]
    if pkg.get("name") and pkg.get("version") and not pkg.get("private"):
        st["build"] += ["mkdir -p dist", "npm pack --pack-destination dist"]
        st["artifact"] = True
    return st


def _stack_maven(root: Path, f) -> dict:
    pins = _pins()
    st = _base("java", "Java (Maven)", "java-maven", "maven")
    pom = f["text"].get("pom.xml", "")
    mvn = "./mvnw" if (root / "mvnw").is_file() else "mvn"
    java = _java_version(f) or pins["runtimes"]["java"]
    st["vars"] = {"java": java}
    st["rt_image"] = _rt_image(pins, "temurin", f"{java}-jdk") if mvn == "./mvnw" else f"maven:3-eclipse-temurin-{java}"
    st["bb_caches"] = ["maven"]
    st["az_vars"] = {"MAVEN_CACHE_DIR": "$(Pipeline.Workspace)/.m2/repository",
                     "MAVEN_OPTS": "-Dmaven.repo.local=$(MAVEN_CACHE_DIR)"}
    b = f"{mvn} -B -ntp"
    if "spotless" in pom:
        st["lint"].append(f"{b} spotless:check")
    if "checkstyle" in pom:
        st["lint"].append(f"{b} checkstyle:check")
    st["test"] = [f"{b} test" + (" jacoco:report" if "jacoco" in pom else "")]
    st["junit"] = "**/target/surefire-reports/TEST-*.xml"
    if f["testcontainers"] or "maven-failsafe-plugin" in pom:
        st["integration"] = [f"{b} verify -Dtest=NONE -Dsurefire.failIfNoSpecifiedTests=false"]
        st["junit_it"] = "**/target/failsafe-reports/TEST-*.xml"
    st["build"] = [f"{b} package -DskipTests", "mkdir -p dist",
                   "find . -path '*/target/*.jar' -not -path './.mvn/*' -exec cp {} dist/ \\;"]
    st["artifact"] = True
    return st


def _stack_gradle(root: Path, f) -> dict:
    pins = _pins()
    st = _base("java", "Java (Gradle)", "java-gradle", "gradle")
    g = f["text"].get("build.gradle", "") + f["text"].get("build.gradle.kts", "")
    gw = "./gradlew" if (root / "gradlew").is_file() else "gradle"
    java = _java_version(f) or pins["runtimes"]["java"]
    st["vars"] = {"java": java}
    st["rt_image"] = _rt_image(pins, "temurin", f"{java}-jdk") if gw == "./gradlew" else f"gradle:jdk{java}"
    st["bb_caches"] = ["gradle"]
    st["az_vars"] = {"GRADLE_USER_HOME": "$(Pipeline.Workspace)/.gradle"}
    lint = [t for cond, t in (("spotless" in g, "spotlessCheck"), ("checkstyle" in g, "checkstyleMain checkstyleTest"),
                              ("ktlint" in g, "ktlintCheck"), ("detekt" in g, "detekt")) if cond]
    if lint:
        st["lint"] = [f"{gw} {' '.join(lint)}"]
    st["test"] = [f"{gw} test" + (" jacocoTestReport" if "jacoco" in g else "")]
    st["junit"] = "**/build/test-results/test/TEST-*.xml"
    if "integrationTest" in g:
        st["integration"] = [f"{gw} integrationTest"]
        st["junit_it"] = "**/build/test-results/integrationTest/TEST-*.xml"
    st["build"] = [f"{gw} assemble", "mkdir -p dist", "find . -path '*/build/libs/*.jar' -exec cp {} dist/ \\;"]
    st["artifact"] = True
    return st


def _stack_go(root: Path, f) -> dict:
    pins = _pins()
    st = _base("go", "Go", "go", "go")
    go = _go_version(f) or pins["runtimes"]["go"]
    st["vars"] = {"go": go}
    st["rt_image"] = _rt_image(pins, "golang", f"{go}-bookworm")
    st["bb_caches"] = ["gomod"]
    st["az_vars"] = {"GOMODCACHE": "$(Pipeline.Workspace)/.go/pkg/mod"}
    gts = f"go run gotest.tools/gotestsum@{pins['tools']['gotestsum']}"
    st["lint"] = ["go vet ./...", 'test -z "$(gofmt -l .)"']
    st["test"] = [f"{gts} --junitfile test-results/junit.xml -- -race -coverprofile=coverage.out ./..."]
    st["junit"] = "test-results/junit.xml"
    sources = [p_ for p_ in list(root.rglob("*.go"))[:400] if not set(p_.relative_to(root).parts) & SKIP_DIRS]
    if any("//go:build integration" in _read(p_, 4000) for p_ in sources):
        st["integration"] = [f"{gts} --junitfile test-results/junit-integration.xml -- -tags=integration ./..."]
        st["junit_it"] = "test-results/junit-integration.xml"
    if any(re.search(r"(?m)^package main\b", _read(p_, 4000)) for p_ in sources):
        st["build"] = ["go build -trimpath -o dist/ ./..."]
        st["artifact"] = True
    return st


def _stack_php(root: Path, f) -> dict:
    pins = _pins()
    st = _base("php", "PHP", "php", "composer")
    try:
        comp = json.loads(f["text"].get("composer.json") or "{}")
    except ValueError:
        comp = {}
    req = dict(comp.get("require") or {}, **(comp.get("require-dev") or {}))
    m = re.search(r"(\d+\.\d+)", str(req.get("php") or ""))
    php = m.group(1) if m else pins["runtimes"]["php"]
    st["vars"] = {"php": php}
    im = pins["images"]["composer"]
    st["rt_image"] = f"{im['name']}:{im['version']}@{im['digest']}"
    st["bb_caches"] = ["composer"]
    st["az_vars"] = {"COMPOSER_CACHE_DIR": "$(Pipeline.Workspace)/.composer"}
    st["install"] = ["composer install --no-interaction --no-progress --prefer-dist"]
    st["install_ci"] = st["install"]
    st["install_steps"] = list(st["install"])
    if "friendsofphp/php-cs-fixer" in req:
        st["lint"] = ["vendor/bin/php-cs-fixer fix --dry-run --diff"]
    elif "squizlabs/php_codesniffer" in req:
        st["lint"] = ["vendor/bin/phpcs"]
    elif "laravel/pint" in req:
        st["lint"] = ["vendor/bin/pint --test"]
    if "phpstan/phpstan" in req:
        st["typecheck"] = ["vendor/bin/phpstan analyse --no-progress"]
    elif "vimeo/psalm" in req:
        st["typecheck"] = ["vendor/bin/psalm --no-progress"]
    if "pestphp/pest" in req:
        st["test"] = ["vendor/bin/pest --log-junit test-results/junit.xml"]
    elif "phpunit/phpunit" in req:
        st["test"] = ["vendor/bin/phpunit --log-junit test-results/junit.xml"]
    st["junit"] = "test-results/junit.xml" if st["test"] else None
    return st


def _jobs_for(st: dict) -> list:
    """Lint (and typecheck), unit tests, integration tests, build: one stack's jobs, fastest first."""
    p = st["prefix"]
    jobs = []
    if st["lint"] or st["typecheck"]:
        steps = ([("Lint", "\n".join(st["lint"]))] if st["lint"] else []) + \
            ([("Typecheck", "\n".join(st["typecheck"]))] if st["typecheck"] else [])
        jobs.append({"id": f"{p}lint", "name": f"{st['label']}: lint" + (" and typecheck" if st["typecheck"] else ""),
                     "needs": [], "timeout": 15, "steps": steps, "stage": "lint", "junit": None, "artifact": None,
                     "commands": st["lint"] + st["typecheck"]})
    first = [jobs[0]["id"]] if jobs else []
    if st["test"]:
        jobs.append({"id": f"{p}test", "name": f"{st['label']}: unit tests", "needs": first, "timeout": 30,
                     "steps": [("Unit tests", "\n".join(st["test"]))], "stage": "test", "junit": st["junit"],
                     "artifact": None, "commands": st["test"]})
    after = [f"{p}test"] if st["test"] else first
    if st["integration"]:
        jobs.append({"id": f"{p}integration", "name": f"{st['label']}: integration tests", "needs": after,
                     "timeout": 30, "steps": [("Integration tests", "\n".join(st["integration"]))],
                     "stage": "integration", "junit": st["junit_it"], "artifact": None, "docker": True,
                     "commands": st["integration"]})
    if st["build"]:
        jobs.append({"id": f"{p}build", "name": f"{st['label']}: build", "needs": after, "timeout": 20,
                     "steps": [("Build", "\n".join(st["build"]))], "stage": "build", "junit": None,
                     "artifact": f"{p}dist" if st["artifact"] else None, "commands": st["build"]})
    for j in jobs:
        j["stack"] = st
    return jobs


TARGETS = {"github": ".github/workflows/ci.yml", "gitlab": ".gitlab-ci.yml", "bitbucket": "bitbucket-pipelines.yml",
           "azure": "azure-pipelines.yml"}  # GitHub also gets .github/workflows/release.yml


def _tag_patterns(policy, root: Path) -> dict:
    fmt = (policy or {}).get("tag_format")
    if not fmt:
        try:
            from . import delivery
            fmt = delivery._scheme_from_tags(delivery._tags(root))[1]
        except Exception:
            fmt = None
    fmt = fmt or "vYY.MM.PATCH"
    v = "v" if fmt.startswith("v") else ""
    return {"tag_format": fmt, "tag_glob": f"{v}[0-9]+.[0-9]+.[0-9]+", "tag_regex": f"/^{v}\\d+\\.\\d+\\.\\d+$/",
            "tag_bb": f"{v}*.*.*", "tag_az": f"{v}*"}


_INTERP = re.compile(r"(^|\s)(?:/usr/bin/|/usr/local/bin/|/opt/homebrew/bin/)?python3?(?:\.\d+)?(?=\s|$)")


def _apply_policy(jobs: list, policy, notes: list) -> None:
    """The delivery policy shapes the generated jobs: its test gate is what the unit-test job runs (the audit checks
    the same thing), and `job_timeout_minutes` is every generated job's timeout."""
    p = policy or {}
    gate = [str(g).strip() for g in (p.get("test_gate") or []) if str(g).strip()]
    tests = [j for j in jobs if j["stage"] == "test"]
    if gate and tests:
        ran = [_norm_cmd(c) for j in jobs if j["stage"] in ("lint", "test") for c in j["commands"]]
        if [g for g in gate if not any(_gate_match(_norm_cmd(g), c) for c in ran)]:
            # one interpreter on the runner: the one the setup step installed (a gate may name the Mac's own pythons)
            run = list(dict.fromkeys(_INTERP.sub(r"\1python", g) for g in gate))
            j = tests[0]
            j.update(steps=[("Test gate (delivery policy)", "\n".join(run))], commands=run, junit=None,
                     name=j["name"].replace("unit tests", "test gate"))
            notes.append(f"the test job runs the delivery policy's test gate: {'; '.join(run)}")
    elif gate:
        notes.append("no test job was generated for the delivery policy's test gate: add "
                     f"`{'; '.join(gate)}` to the pipeline")
    mins = p.get("job_timeout_minutes")
    if isinstance(mins, int) and mins > 0:
        for j in jobs:
            j["timeout"] = mins


def generate(root, provider: str = "github", stack: str = "auto", policy=None, branch=None) -> dict:
    """The pipeline files for the repository -> ({path: text}, notes)."""
    root = Path(root)
    pins = _pins()
    f = facts(root)
    stacks = detect_stacks(root, stack, f)
    notes = []
    docker = bool(f["dockerfiles"] and any(Path(d).parent == Path(".") for d in f["dockerfiles"])) or stack == "docker"
    if not stacks and not docker:
        notes.append("no stack detected from the repository root: the pipeline has only the security and release "
                     "jobs; pass --stack to choose one")
    branch = branch or core.gitout(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").replace("origin/", "") \
        or "main"
    ctx = {"project": root.name, "date": dt.date.today().isoformat(), "branch": branch, **_tag_patterns(policy, root)}
    jobs = [j for s_ in stacks for j in _jobs_for(s_)]
    _apply_policy(jobs, policy, notes)
    compose = f["compose_test"]
    if compose and provider in ("github", "azure") and not any(j["stage"] == "integration" for j in jobs):
        svc = _compose_service(root / compose[0])
        cmd = f"docker compose -f {compose[0]} up --build --abort-on-container-exit" + \
            (f" --exit-code-from {svc}" if svc else "")
        jobs.append({"id": "integration", "name": "Integration tests (docker compose)",
                     "needs": [j["id"] for j in jobs if j["stage"] == "test"], "timeout": 30,
                     "steps": [("Integration tests", cmd)], "stage": "integration", "junit": None, "artifact": None,
                     "docker": True, "commands": [cmd], "stack": None})
    elif compose:
        notes.append(f"{compose[0]}: run it as an integration job by hand (needs a Docker service on {provider})")
    for s_ in stacks:
        if not s_["test"]:
            notes.append(f"{s_['label']}: no tests found, so no test job; add tests and run `dev ci init --force`")
    if provider == "github":
        # draft pull requests run nothing; "ready for review" starts the one run (spec P-fy3bwa0pshk R6)
        files = {".github/workflows/ci.yml": draft_gate(_render_github(ctx, jobs, stacks, docker, pins))[0],
                 ".github/workflows/release.yml": _render_github(ctx, jobs, stacks, docker, pins, release=True)}
        if not f["updater"]:
            files[".github/dependabot.yml"] = _render_dependabot(ctx, stacks, docker, pins)
            notes.append("added .github/dependabot.yml: weekly, grouped updates keep the pinned actions current")
        return files, notes
    if provider == "gitlab":
        text = _render_gitlab(ctx, jobs, stacks, docker, pins)
    elif provider == "bitbucket":
        text = _render_bitbucket(ctx, jobs, stacks, docker, pins)
    else:
        text = _render_azure(ctx, jobs, stacks, docker, pins)
    return {TARGETS[provider]: text}, notes


def _render_dependabot(ctx, stacks, docker, pins) -> str:
    eco = ["github-actions"]
    for st in stacks:
        eco.append({"python": "uv" if st["pm"] == "uv" else "pip", "node": "npm", "go": "gomod",
                    "php": "composer"}.get(st["key"], "maven" if st["pm"] == "maven" else "gradle"))
    if docker:
        eco.append("docker")
    entries = [render(_tpl("github/dependabot-entry.yml"), {"ecosystem": e, "group": e.replace("-", "_")}, pins)
               for e in dict.fromkeys(eco)]
    return render(_tpl("github/dependabot.yml"), dict(ctx, updates="\n".join(x.rstrip("\n") for x in entries)), pins)


def _compose_service(p: Path):
    try:
        t = load_yaml(_read(p))
    except YamlError:
        return None
    svcs = list((t or {}).get("services") or {}) if isinstance(t, dict) else []
    return next((x for x in svcs if x in ("sut", "test", "tests", "integration", "it", "e2e")), svcs[0] if svcs else None)


def _release_needs(jobs, docker, image_id="image", security="security"):
    needs = [j["id"] for j in jobs if j["stage"] in ("build", "integration")] or [j["id"] for j in jobs if
                                                                                  j["stage"] == "test"]
    return needs + [security] + ([image_id] if docker else [])


def _render_github(ctx, jobs, stacks, docker, pins, release=False) -> str:
    """ci.yml (pull requests and the default branch, cached) or release.yml (tags: the gate again, uncached)."""
    if release:  # a cache must never feed what is released (zizmor: cache-poisoning); lint already ran on the PR
        ids = {j["id"] for j in jobs if j["stage"] != "lint"}
        jobs = [dict(j, needs=[n for n in j["needs"] if n in ids]) for j in jobs if j["id"] in ids]
    out = []
    setups = {}
    for j in jobs:
        st = j["stack"]
        if st is None:
            setup = render("      - uses: @{uses:actions/checkout}\n        with:\n          persist-credentials: false\n",
                           {}, pins)
        else:
            if st["key"] not in setups:
                v = dict(st["vars"], install="\n".join(st["install"]), cache=not release)
                setups[st["key"]] = render(_tpl(f"github/setup/{st['setup']}.yml"), v, pins)
            setup = setups[st["key"]]
        steps = "".join(render(_tpl("github/step.yml"), {"name": _yq(n), "run": c}, pins) for n, c in j["steps"])
        out.append(render(_tpl("github/job.yml"), {"id": j["id"], "name": _yq(j["name"]), "needs": ", ".join(j["needs"]),
                                                   "timeout": j["timeout"], "setup": setup.rstrip("\n"),
                                                   "steps": steps.rstrip("\n"), "junit": _yq(j["junit"]) if j["junit"]
                                                   else None, "artifact": j["artifact"]}, pins))
    out.append(render(_tpl("github/security.yml"), {}, pins))
    if docker and not release:
        out.append(render(_tpl("github/docker.yml"), {"needs": ", ".join(j["id"] for j in jobs if j["stage"] == "test")},
                          pins))
    if release:
        out.append(render(_tpl("github/release.yml"), dict(ctx, needs=", ".join(_release_needs(jobs, False)),
                                                           artifacts=any(j["artifact"] for j in jobs), docker=docker),
                          pins))
    title = f"{'Release' if release else 'CI'} for {ctx['project']}"
    return render(_tpl("github/pipeline.yml"), dict(ctx, jobs="\n\n".join(x.rstrip("\n") for x in out), title=title,
                                                    release_wf=release, wf_name="release" if release else "ci"), pins)


def _render_gitlab(ctx, jobs, stacks, docker, pins) -> str:
    setups = []
    for st in stacks:
        v = dict(st["vars"], key=st["key"], rt_image=st["rt_image"], install=" && ".join(st["install"]) or "true",
                 install_ci=" && ".join(st["install_ci"] or st["install"]) or "true",
                 install_list="\n".join(_yaml_item(c) for c in st["install"]))
        setups.append(render(_tpl(f"gitlab/setup/{st['setup']}.yml"), v, pins))
    out = []
    for j in jobs:
        if j["stack"] is None:
            continue
        script = "\n".join(_yaml_item(c) for c in j["commands"])
        out.append(render(_tpl("gitlab/job.yml"), {"id": j["id"], "stage": j["stage"], "key": j["stack"]["key"],
                                                   "needs": ", ".join(j["needs"]), "docker": j.get("docker"),
                                                   "script": script, "junit": _yq(j["junit"]) if j["junit"] else None,
                                                   "artifact": j["artifact"]}, pins))
    out.append(render(_tpl("gitlab/security.yml"), {}, pins))
    if docker:
        out.append(render(_tpl("gitlab/docker.yml"), {"needs": ", ".join(j["id"] for j in jobs if j["stage"] == "test")},
                          pins))
    needs = _release_needs(jobs, docker, "container-scan", "secret-scan") + ["sast", "dependency-scan",
                                                                            "filesystem-and-iac-scan"]
    out.append(render(_tpl("gitlab/release.yml"), {"needs": ", ".join(needs)}, pins))
    return render(_tpl("gitlab/pipeline.yml"), dict(ctx, setups="\n".join(x.rstrip("\n") for x in setups),
                                                    jobs="\n\n".join(x.rstrip("\n") for x in out)), pins)


def _render_bitbucket(ctx, jobs, stacks, docker, pins) -> str:
    steps, flow_groups = [], {"lint": [], "test": [], "integration": [], "build": []}
    for j in jobs:
        st = j["stack"]
        if st is None:
            continue
        cmds = (st.get("install_steps") or []) + j["commands"]
        steps.append(render(_tpl("bitbucket/step.yml"), {
            "id": j["id"], "name": _yq(j["name"]), "rt_image": st["rt_image"], "caches": ", ".join(
                st["bb_caches"] + (["docker"] if j.get("docker") else [])), "docker": j.get("docker"),
            "script": "\n".join(_yaml_item(c) for c in cmds), "artifact": j["artifact"]}, pins))
        flow_groups[j["stage"]].append(j["id"])
    steps.append(render(_tpl("bitbucket/security.yml"), {}, pins))
    flow_groups["lint"] = ["secret-scan", "sast", "dependency-scan", "filesystem-scan"] + flow_groups["lint"]
    if docker:
        steps.append(render(_tpl("bitbucket/docker.yml"), {}, pins))
        flow_groups["build"].append("container-image")
    steps.append(render(_tpl("bitbucket/release.yml"), {}, pins))
    flow = []
    for stage in ("lint", "test", "integration", "build"):
        ids = flow_groups[stage]
        if len(ids) == 1:
            flow.append(f"      - step: *{ids[0]}")
        elif ids:
            flow.append("      - parallel:\n" + "\n".join(f"          - step: *{i}" for i in ids))
    release_flow = "      - step: *release-sbom\n      - step: *release"
    caches = _tpl("bitbucket/setup/caches.yml").rstrip("\n")
    return render(_tpl("bitbucket/pipeline.yml"), dict(ctx, caches=caches, steps="\n\n".join(
        x.rstrip("\n") for x in steps), flow="\n".join(flow), release_flow=release_flow), pins)


def _render_azure(ctx, jobs, stacks, docker, pins) -> str:
    def aid(x):
        return x.replace("-", "_")
    variables = {}
    for st in stacks:
        variables.update(st["az_vars"])
    out, setups = [], {}
    for j in jobs:
        st = j["stack"]
        if st is None:
            setup = ""
        else:
            if st["key"] not in setups:
                v = dict(st["vars"], install="\n".join(st["install"]),
                         install_ci="\n".join(st["install_ci"] or st["install"]), lockfile=st["vars"].get("lockfile", ""),
                         pip_key=st["vars"].get("pip_key", ""))
                setups[st["key"]] = render(_tpl(f"azure/setup/{st['setup']}.yml"), v, pins)
            setup = setups[st["key"]]
        steps = "".join(render(_tpl("azure/step.yml"), {"name": _yq(n), "run": c}, pins) for n, c in j["steps"])
        out.append(render(_tpl("azure/job.yml"), {"id": aid(j["id"]), "name": _yq(j["name"]),
                                                  "needs": ", ".join(aid(n) for n in j["needs"]),
                                                  "timeout": j["timeout"], "setup": setup.rstrip("\n"),
                                                  "steps": steps.rstrip("\n"), "junit": j["junit"],
                                                  "artifact": aid(j["artifact"]) if j["artifact"] else None}, pins))
    out.append(render(_tpl("azure/security.yml"), {}, pins))
    if docker:
        out.append(render(_tpl("azure/docker.yml"), {"needs": ", ".join(aid(j["id"]) for j in jobs
                                                                       if j["stage"] == "test")}, pins))
    release = render(_tpl("azure/release.yml"), dict(ctx, artifacts=any(j["artifact"] for j in jobs)), pins)
    var_text = "\n".join(f"  {k}: {v}" for k, v in variables.items()) or "  CI: \"true\""
    return render(_tpl("azure/pipeline.yml"), dict(ctx, variables=var_text, jobs="\n\n".join(x.rstrip("\n") for x in out),
                                                   release=release.rstrip("\n")), pins)


# =========================================================================== CLI

def add_ci_arguments(p):
    """`dev ci audit` and `dev ci init` options (cli.py calls this next to gitx.add_ci_arguments)."""
    have = getattr(p, "_option_string_actions", {})
    g = p.add_argument_group("pipelines: dev ci audit [--fix] · dev ci init")

    def opt(*names, **kw):
        if not any(n in have for n in names):
            g.add_argument(*names, **kw)
    opt("--provider", choices=PROVIDERS, help="audit or init: one host (default: every pipeline; init: from the remote)")
    opt("--fix", action="store_true", help="audit: apply the safe GitHub Actions fixes in the working tree (never "
                                           "commits); print the snippets for other hosts")
    opt("--strict", action="store_true", help="audit: exit 1 on medium findings too (default: high and critical)")
    opt("--stack", choices=("auto", "python", "node", "java-maven", "java-gradle", "go", "php", "docker"),
        default="auto", help="init: the stack (default: detected from the lockfiles)")
    opt("--force", action="store_true", help="init: overwrite an existing pipeline file")
    opt("--dry-run", action="store_true", help="init: print the files instead of writing them")
    return p


def _policy(root: Path):
    try:
        from . import delivery
        return delivery.load(delivery.scope_for(root))
    except Exception:
        return None


def _visibility(root: Path):
    try:
        from . import cistatus
        prov = cistatus.provider_for(core.gitout(root, "config", "--get", "remote.origin.url"))
        if not prov or prov[0] != "github" or not _have("gh"):
            return None
        rc, out, _ = _run(["gh", "repo", "view", prov[2], "--json", "visibility", "-q", ".visibility"], timeout=15)
        return out.strip().lower() if rc == 0 and out.strip() else None
    except Exception:
        return None


def _provider_from_remote(root: Path):
    try:
        from . import cistatus
        prov = cistatus.provider_for(core.gitout(root, "config", "--get", "remote.origin.url"))
        return prov[0] if prov else None
    except Exception:
        return None


SEV_STYLE = {"critical": "red", "high": "red", "medium": "yellow", "low": "cyan", "info": "dim"}
NEXT_STEPS = ("dev branch new ci/<topic>", "dev commit", "dev ci --run", "dev push", "dev pr create")


def render_report(rep: dict, ui, fixed=None, show_snippets=False) -> str:
    out = [ui.section(f"CI/CD audit · {core.tilde(rep['root'])}")]
    for prov, files in rep["providers"].items():
        out.append(f"  {ui.bold(PROVIDER_NAMES.get(prov, prov))}  {ui.dim(', '.join(files))}")
    if rep["tools"]:
        out.append("  " + ui.dim(" · ".join(f"{k}: {v}" for k, v in rep["tools"].items())))
    for n in rep.get("notes", []):
        out.append("  " + ui.dim(n))
    s = rep["summary"]
    counts = " · ".join(f"{k} {s[k]}" for k in SEVERITIES)
    out.append("")
    out.append(f"  {counts}" + (f"  ·  {s['fixable']} fixable with --fix" if s["fixable"] else ""))
    if fixed:
        out.append(ui.section("Fixed (working tree, not committed)"))
        for a in fixed["applied"] or ["nothing to change"]:
            out.append(f"  {ui.OK} {a}")
        for k in fixed["skipped"]:
            out.append(f"  {ui.WARN} {k}")
    for cat in CATEGORIES:
        fs = [f for f in rep["findings"] if f["category"] == cat]
        if not fs:
            continue
        out.append(ui.section(cat.capitalize()))
        for f in fs:
            color = getattr(ui, SEV_STYLE.get(f["severity"], "dim"))
            where = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
            tag = "  [--fix]" if f["autofix"] else ""
            also = f" (also {', '.join(f['also'])})" if f.get("also") else ""
            out.append(f"  {color(f['severity'].upper().ljust(8))} {where}  {ui.bold(f['id'])}{ui.dim(tag + also)}")
            out.append(f"           {f['message']}")
            if f.get("fix"):
                out.append(f"           {ui.dim('fix: ' + f['fix'])}")
            if show_snippets and f.get("snippet") and f["provider"] != "github":
                for ln in f["snippet"].splitlines():
                    out.append(f"             {ui.cyan(ln)}")
    if not rep["findings"]:
        out.append(f"\n  {ui.OK} no findings")
    return "\n".join(out)


def cmd_audit(a, top) -> int:
    from . import ui
    root = Path(top)
    policy = _policy(root)
    provider = getattr(a, "provider", None)
    rep = audit(root, provider=provider, policy=policy, visibility=lambda: _visibility(root))
    fixed = None
    if getattr(a, "fix", False):
        fixed = apply_fixes(root, rep, policy=policy)
        if fixed["changed"]:
            rep = audit(root, provider=provider, policy=policy, visibility=lambda: _visibility(root))
    code = exit_code(rep, getattr(a, "strict", False))
    if getattr(a, "json", False):
        print(json.dumps(dict(rep, fixed=fixed, exit=code), ensure_ascii=False, indent=1))
        return code
    print(render_report(rep, ui, fixed, show_snippets=bool(fixed is not None)))
    if fixed is not None or rep["summary"]["fixable"]:
        print(ui.section("Next"))
        if fixed is None:
            print(f"  dev ci audit --fix   {ui.dim('applies the [--fix] items to the working tree')}")
        print("  " + " · ".join(NEXT_STEPS))
    print()
    return code


def cmd_init(a, top) -> int:
    from . import ui
    root = Path(top)
    provider = getattr(a, "provider", None) or _provider_from_remote(root) or "github"
    policy = _policy(root)
    files, notes = generate(root, provider, getattr(a, "stack", "auto") or "auto", policy)
    dry = getattr(a, "dry_run", False)
    exists = [rel for rel in files if (root / rel).exists()]
    if exists and not getattr(a, "force", False) and not dry:
        ui.err(f"{', '.join(exists)} already exists: `dev ci audit` diagnoses it; `dev ci init --force` replaces it "
               f"(see it first with --dry-run)")
        return 1
    if dry:
        for rel, text in files.items():
            print(ui.section(f"{rel} (dry run, not written)"))
            print(text)
    else:
        for rel, text in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            print(f"{ui.OK} wrote {rel} ({PROVIDER_NAMES[provider]})")
    for n in notes:
        print(f"  {ui.INFO} {n}")
    rep = audit(root, provider=provider, policy=policy, only=set(files), overlay=files if dry else None,
                external=not dry, visibility=lambda: _visibility(root))
    bad = [f for f in rep["findings"] if RANK.get(f["severity"], 9) <= RANK["medium"]]
    if bad:
        print(render_report(rep, ui))
    else:
        low = len(rep["findings"])
        print(f"{ui.OK} audit: clean (no medium or high findings" + (f"; {low} low or info" if low else "") + ")" +
              ("" if dry else f" · {'; '.join(f'{k}: {v}' for k, v in rep['tools'].items())}" if rep["tools"] else ""))
    if provider == "github" and not dry:
        if _have("actionlint"):
            wfs = [x for x in files if x.startswith(".github/workflows/")]
            rc, o, e = _run(["actionlint", *wfs], cwd=str(root), timeout=120)
            print(f"{ui.OK if rc == 0 else ui.FAIL} actionlint: " + ("clean" if rc == 0 else (o or e)[:400]))
            bad = bad or rc != 0
        else:
            print(f"  {ui.dim('actionlint not installed (brew install actionlint): GitHub syntax not checked')}")
    print(ui.section("Next"))
    print("  " + " · ".join(NEXT_STEPS))
    print()
    return 1 if bad else 0
