# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Change fragments: each branch writes its CHANGELOG lines in a file of its own, and the release writes them into
CHANGELOG.md.

CHANGELOG.md is the one file every change edits, so branches that run in parallel collide on it even when their code
does not. A repository that keeps a `changes/` folder takes its CHANGELOG lines from fragments instead:

- each branch adds `changes/<branch>.md` (its name with `/` as `-`, such as changes/feat-invoice-export.md) with Keep
  a Changelog headings (`### Added`, `### Changed`, `### Deprecated`, `### Removed`, `### Fixed`, `### Security`)
  and `- ` entries under them; an entry's next lines are indented by two spaces;
- `dev release` on a release branch (release/<version>, hotfix/…, integration/…) writes every fragment into that
  version's section of CHANGELOG.md, its sections in Keep a Changelog's order, and removes the fragment files; it
  tags nothing there. On the default branch it refuses while a fragment is left (prepare_release, release_refusal);
- `dev ci --run` and `dev ci --mark-passed` fail a branch that changes CHANGELOG.md outside a release branch, and a
  fragment they cannot read, a symbolic link or a file in changes/ that is no fragment (ci_problem).

`changes/README.md` is no fragment: it says how to write one, and it keeps the folder, which turns the checks on,
after a release has removed every fragment.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import core

FOLDER = "changes"
CHANGELOG = "CHANGELOG.md"
SECTIONS = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
RELEASE_PREFIXES = ("release/", "hotfix/", "integration/")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_ENTRY = re.compile(r"^[-*+]\s+\S")
_COMMENT = re.compile(r"^\s*<!--.*-->\s*$")
_VERSION = re.compile(r"v?(\d+\.\d+\.\d+[0-9A-Za-z.+-]*)")
_RELEASE = re.compile(r"^##\s+\[?v?\d+(?:\.\d+)+")  # a released version's heading: ## [26.11.27] - 2026-11-20
_LINK_DEF = re.compile(r"^ {0,3}\[[^\]]+\]:\s*\S")  # a link reference definition: [26.11.27]: https://…
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_BOM = b"\xef\xbb\xbf"  # UTF-8's byte-order mark


class FragmentError(ValueError):
    """A fragment devkit cannot read, or an entry of changes/ that is no fragment: its path, the line (0: the whole
    entry), and why."""

    def __init__(self, where: str, line: int, reason: str):
        super().__init__(f"{where}:{line}: {reason}" if line else f"{where}: {reason}")
        self.where, self.line, self.reason = where, line, reason


def uses_fragments(top) -> bool:
    """Whether the repository keeps a changes/ folder, which turns fragments and their checks on."""
    return (Path(top) / FOLDER).is_dir()


def release_branch(branch) -> bool:
    """A branch where the CHANGELOG is written: release/<version>, hotfix/<version-or-topic>, integration/<topic>."""
    return str(branch or "").startswith(RELEASE_PREFIXES)


def branch_version(branch) -> str:
    """The version a release or hotfix branch names (release/26.11.28 -> 26.11.28), or ""."""
    head, _, rest = str(branch or "").partition("/")
    m = _VERSION.fullmatch(rest) if head in ("release", "hotfix") else None
    return m.group(1) if m else ""


def fragment_path(branch) -> str:
    """The fragment a branch writes: changes/<its name with / as ->.md."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(branch or "")).strip("-.")
    return f"{FOLDER}/{name or 'change'}.md"


def _entries(top) -> list:
    """Every entry under changes/ but its folders, README.md files and hidden names -> [(path from the repository top,
    Path)], sorted. A symbolic link is an entry whatever it points to, and a linked folder is not walked into."""
    root = Path(top) / FOLDER
    out = []
    for here, dirs, files in os.walk(root):
        base = Path(here)
        links = [d for d in dirs if (base / d).is_symlink()]
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in links]
        for name in files + links:
            if not name.startswith(".") and name.lower() != "readme.md":
                out.append((f"{FOLDER}/{(base / name).relative_to(root).as_posix()}", base / name))
    return sorted(out)


def fragments(top) -> list:
    """The fragments of the working tree, as paths from the repository top, sorted: every entry under changes/ named
    *.md except README.md and hidden ones (read refuses one that is a symbolic link)."""
    return [rel for rel, _ in _entries(top) if rel.endswith(".md")]


def parse(text: str, where: str = "") -> dict:
    """A fragment's entries by section -> {"Added": ["- entry\\n  its next line", …], …}. Raises FragmentError, naming
    `where` and the line, for a line before the first section heading, a heading that is no Keep a Changelog section,
    text outside a list entry, or a fragment with no entry at all. `*` and `+` bullets become `-`; an entry's lines
    that are not indented (a lazy continuation) are indented by two spaces; whole-line HTML comments are left out."""
    found, section, entry, blank = {}, None, None, False
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip() or _COMMENT.match(line):
            blank = blank or not line.strip()
            continue
        indented = raw[:1].isspace()
        h = None if indented else _HEADING.match(line)
        if h:
            name = h.group(2).strip("[] ").capitalize()
            if len(h.group(1)) not in (2, 3) or name not in SECTIONS:
                raise FragmentError(where, n, f"“{line}” is not a Keep a Changelog section: use "
                                    + ", ".join(f"### {s}" for s in SECTIONS))
            section, entry, blank = found.setdefault(name, []), None, False
            continue
        if section is None:
            raise FragmentError(where, n, "a line before the first section heading: start with ### Added, "
                                          "### Fixed or another Keep a Changelog section")
        if not indented and _ENTRY.match(line):
            entry = ["- " + line[1:].lstrip()]
            section.append(entry)
        elif entry is not None and (indented or not blank):
            entry += ([""] if blank else []) + [line if indented else "  " + line]
        else:
            raise FragmentError(where, n, "text outside a list entry: start an entry with `- ` and indent its "
                                          "next lines")
        blank = False
    entries = {name: ["\n".join(e) for e in items] for name, items in found.items() if items}
    if not entries:
        raise FragmentError(where, 1, "no entries: write `- ` lines under a section heading, or delete the file")
    return entries


def _utf8(data: bytes):
    """`data` decoded as strict UTF-8 without a leading byte-order mark -> (text, None), or (None, (line, why)) for
    the first byte that is not UTF-8."""
    body = data[len(_BOM):] if data.startswith(_BOM) else data
    try:
        return body.decode("utf-8"), None
    except UnicodeDecodeError as e:
        return None, (body[:e.start].count(b"\n") + 1, f"not UTF-8 (byte 0x{body[e.start]:02x})")


def read(top) -> list:
    """[(fragment path, its entries by section)] for every fragment, in path order. Raises FragmentError for a fragment
    it cannot parse or that is not UTF-8 (a leading byte-order mark is dropped), and for an entry of changes/ that is
    a symbolic link (changes/ itself too) or no fragment (a .md file); README.md and hidden names are left alone."""
    if (Path(top) / FOLDER).is_symlink():
        raise FragmentError(FOLDER, 0, "a symbolic link: keep changes/ as a folder of this repository")
    out = []
    for rel, f in _entries(top):
        if f.is_symlink():
            raise FragmentError(rel, 0, "a symbolic link: write the change fragment as a file of its own")
        if not (rel.endswith(".md") and f.is_file()):
            raise FragmentError(rel, 0, "not a change fragment, which is a .md file: rename it, or move it out of "
                                        f"{FOLDER}/")
        text, bad = _utf8(f.read_bytes())
        if bad:
            raise FragmentError(rel, bad[0], bad[1] + ": save the fragment as UTF-8")
        out.append((rel, parse(text, rel)))
    return out


def _trim(lines: list) -> list:
    """`lines` without the blank lines at either end."""
    lo, hi = 0, len(lines)
    while lo < hi and not lines[lo].strip():
        lo += 1
    while hi > lo and not lines[hi - 1].strip():
        hi -= 1
    return lines[lo:hi]


def _fenced(lines: list) -> list:
    """For each line, whether it belongs to a fenced code block, its fences included: a heading there is code."""
    out, fence = [], ""
    for ln in lines:
        m = _FENCE.match(ln)
        if fence:
            out.append(True)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
                fence = ""
        elif m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = m.group(1)
            out.append(True)
        else:
            out.append(False)
    return out


def _section(heading: str, body: list, parts: list) -> list:
    """A version's section rebuilt with the fragments' entries: the lines before its first `###` subsection stay,
    each subsection keeps its lines and takes the new entries after them, and a missing Keep a Changelog subsection
    is added before the first one that comes after it in that order. Blank lines are normalized: one after each
    heading and between subsections, and before new entries that follow a fenced code block. A `###` line in a fenced
    code block is code, not a subsection."""
    intro, subs, current = [], [], None
    for ln, code in zip(body, _fenced(body)):
        m = None if code else re.match(r"^###\s+(.*?)\s*$", ln)
        if m:
            current = [m.group(1), []]
            subs.append(current)
        elif current is None:
            intro.append(ln)
        else:
            current[1].append(ln)

    def rank(name):
        key = name.strip("[] ").capitalize()
        return SECTIONS.index(key) if key in SECTIONS else None

    for name in SECTIONS:
        new = [ln for p in parts for entry in p.get(name, []) for ln in entry.split("\n")]
        if not new:
            continue
        sub = next((s for s in subs if rank(s[0]) == SECTIONS.index(name)), None)
        if sub is None:
            sub = [name, []]
            later = next((k for k, s in enumerate(subs)
                          if rank(s[0]) is not None and rank(s[0]) > SECTIONS.index(name)), len(subs))
            subs.insert(later, sub)
        kept = _trim(sub[1])
        sub[1] = kept + ([""] if kept and _fenced(kept)[-1] else []) + new  # a code block ends: a blank line
    out = [heading, ""]
    if _trim(intro):
        out += _trim(intro) + [""]
    for name, lines in subs:
        out += [f"### {name}", ""] + _trim(lines) + [""]
    return _trim(out)


def _before_links(lines: list, code: list, lo: int, hi: int) -> int:
    """Where the blank lines and link reference definitions that end lines[lo:hi] begin (hi when none do); a line in
    a fenced code block (`code`, _fenced's) is no definition."""
    while hi > lo and not code[hi - 1] and (not lines[hi - 1].strip() or _LINK_DEF.match(lines[hi - 1])):
        hi -= 1
    return hi


def merge(changelog: str, version: str, date: str, parts: list) -> str:
    """CHANGELOG text with the fragments' entries (`parts`, parse's output, in order) in `version`'s section. A
    section the CHANGELOG has already keeps its heading and lines; a new one, `## [<version>] - <date>`, goes above
    the newest release (the first `##` heading that names a version), below an Unreleased section and any other `##`
    heading; with no release yet, at the end, before the link definitions there. The link definitions at the end of
    the file stay there when the version's section is the last one, too. Headings in fenced code blocks are code.
    The text keeps the line ending of the CHANGELOG's first line: \\r\\n or \\n."""
    first = re.search(r"\r?\n", changelog)
    newline = first.group(0) if first else "\n"
    lines = changelog.replace("\r\n", "\n").split("\n")
    code = _fenced(lines)
    head = re.compile(r"^##\s*\[?v?" + re.escape(version) + r"\]?(\s|$)")
    start = next((i for i, ln in enumerate(lines) if not code[i] and head.match(ln)), None)
    if start is None:
        at = next((i for i, ln in enumerate(lines) if not code[i] and _RELEASE.match(ln)), None)
        if at is None:
            at = _before_links(lines, code, 0, len(lines))
        before, heading, body, after = lines[:at], f"## [{version}] - {date}", [], lines[at:]
    else:
        end = next((j for j in range(start + 1, len(lines)) if not code[j] and re.match(r"^#{1,2}\s", lines[j])),
                   len(lines))
        if end == len(lines):  # the last section: the link definitions that end the file are not its lines
            end = _before_links(lines, code, start + 1, end)
        before, heading, body, after = lines[:start], lines[start], lines[start + 1:end], lines[end:]
    before, after = _trim(before), _trim(after)
    out = (before + [""] if before else []) + _section(heading, body, parts) + ([""] + after if after else [])
    return newline.join(out) + newline


def release_refusal(top, version: str):
    """Why `dev release` on the default branch may not tag yet -> (reason, fix), or None: a fragment is left, so its
    lines would ship without a place in the CHANGELOG."""
    left = fragments(top)
    if not left:
        return None
    shown = ", ".join(left[:5]) + (f" and {len(left) - 5} more" if len(left) > 5 else "")
    return (f"{len(left)} change fragment(s) are not in {CHANGELOG} yet: {shown}",
            f"on release/{version}: dev release writes them into {CHANGELOG} and removes them; merge it, then release")


def prepare_release(c, a, today: str = "") -> int:
    """`dev release` on a release branch of a repository that keeps change fragments: write them into the version's
    section of CHANGELOG.md and remove them, both staged for the release commit. It tags and pushes nothing. The
    version is --version, else the one the branch names; a branch that names none (integration/<topic>,
    hotfix/<topic>) needs --version, and a --version that differs from the branch's is refused. The version is checked
    as dev release on the default branch checks the tag's, its tag included (gitx.release_version_refusal), so a
    version already released is refused before its shipped section takes new lines. With no fragment left it refuses
    and says
    what comes next: commit the staged CHANGELOG.md, or merge the branch and release on the default branch. `a`: the
    parsed `dev release` arguments (version, dry_run). -> the exit code."""
    from . import gitx
    try:
        parts = read(c.top)
    except FragmentError as e:
        return gitx.refuse(str(e), "fix it, then: dev release")
    given, named = (getattr(a, "version", None) or "").lstrip("v"), branch_version(c.branch)
    if not parts:
        if core.git(c.top, "diff", "--cached", "--quiet", "--", CHANGELOG)[0] == 1:
            return gitx.refuse(f"no change fragments left on {c.branch}: commit the staged {CHANGELOG}",
                               f"commit it (chore(release): {given or named or '<version>'}), dev ci --run, dev push, "
                               f"dev pr create; after the merge, dev release on {c.default} tags it")
        return gitx.refuse(f"no change fragments on {c.branch} to write into {CHANGELOG}; releases are tagged on "
                           f"`{c.default}`", f"merge the release PR (dev pr merge), then: git switch {c.default} && "
                                             "dev rebase && dev release")
    if given and named and given != named:
        return gitx.refuse(f"--version {given} differs from the version the branch names ({c.branch})",
                           f"dev release --version {named}, or release {given} from release/{given}")
    version = given or named
    scheme, _, nxt = gitx.version_scheme(c.top)
    if not version:
        return gitx.refuse(f"{c.branch} names no version: say which version its change fragments go into",
                           f"dev release --version {nxt if scheme == 'calver' else 'X.Y.Z'}")
    bad = gitx.release_version_refusal(c.top, version, scheme, nxt)
    if bad:
        return gitx.refuse(*bad)
    names = [rel for rel, _ in parts]
    path = Path(c.top) / CHANGELOG
    data = path.read_bytes() if path.is_file() else b"# Changelog\n"
    old, bad = _utf8(data)
    if bad:
        return gitx.refuse(f"{CHANGELOG} is {bad[1]} on line {bad[0]}: devkit writes it as UTF-8",
                           f"save {CHANGELOG} as UTF-8, then: dev release")
    bom = "\ufeff" if data.startswith(_BOM) else ""  # the file keeps its byte-order mark
    new = bom + merge(old, version, today or time.strftime("%Y-%m-%d"), [p for _, p in parts])
    if getattr(a, "dry_run", False):
        gitx.ok(f"dry run: {len(names)} change fragment(s) would go into {CHANGELOG} [{version}] and be removed; "
                "nothing is tagged on a release branch")
        for rel in names:
            print(f"  → {rel}")
        return 0
    path.write_bytes(new.encode("utf-8"))  # as merge wrote it: no newline translation
    tracked = set(core.gitout(c.top, "ls-files", "-z", "--", FOLDER).split("\0"))
    for rel in names:
        if rel in tracked:
            rc, _, err = core.git(c.top, "rm", "-q", "-f", "--", rel)
            if rc != 0:
                return gitx.fail(f"{CHANGELOG} is written, but git rm {rel} failed", err)
        else:
            (Path(c.top) / rel).unlink()
    core.git(c.top, "add", "--", CHANGELOG)
    gitx.ok(f"wrote {len(names)} change fragment(s) into {CHANGELOG} [{version}] and removed them: "
            + ", ".join(names))
    gitx.info("nothing is tagged on a release branch: review the section, bump the version files, commit "
              f"(chore(release): {version}), dev ci --run, dev push, dev pr create; after the merge, dev release on "
              "the default branch tags it")
    return 0


def ci_problem(top, branch):
    """Why `dev ci --run` fails here -> (reason, detail), or None. Only in a repository that keeps a changes/ folder:
    a fragment devkit cannot read, or, on a branch that is not a release branch, CHANGELOG.md changed against the
    branch's base (testx.base_ref: the merge-base with the default branch, or the unmerged release or integration
    branch it was cut from), committed or not."""
    if not uses_fragments(top):
        return None
    try:
        read(top)
    except FragmentError as e:
        return (str(e), f"{FOLDER}/ holds change fragments, README.md and hidden files only; a fragment is a .md file "
                        "with Keep a Changelog headings (### Added, ### Fixed …) and `- ` entries under them")
    if release_branch(branch):
        return None
    from . import testx
    base, label = testx.base_ref(top)
    if not base:
        return None
    rc, _, _ = core.git(top, "diff", "--quiet", base, "--", CHANGELOG)
    new = core.gitout(top, "ls-files", "--others", "--exclude-standard", "--", CHANGELOG)
    if rc not in (0, 1) or (rc == 0 and not new):
        return None
    where = core.gitout(top, "log", "--format=%h %s", f"{base}..HEAD", "--", CHANGELOG).splitlines()[:5]
    if new or core.git(top, "diff", "--quiet", "HEAD", "--", CHANGELOG)[0] == 1:
        where.append("uncommitted changes")
    name = branch or "this branch"
    return (f"{CHANGELOG} is changed on {name} (since {label}): this repository takes its CHANGELOG lines from change "
            f"fragments, which a release writes into it",
            "\n".join(where + [f"move the lines to {fragment_path(branch or 'change')} and restore {CHANGELOG}: "
                               f"git checkout {base[:12]} -- {CHANGELOG}"]))
