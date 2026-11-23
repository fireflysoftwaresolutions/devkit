# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Only the bug registry's workflow may write issues. The registry trusts the newest record comment that
github-actions[bot] wrote on an issue (tools/bug_registry_action.py), and every workflow of this repository posts as
that account, so any other workflow with issues: write (or a token that can comment on issues) could forge a record.
A workflow without a top-level permissions block takes the repository's default token, which can be read and write
on everything, so each workflow must declare one.
Run: python3 -m unittest discover -s tests -t ."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
REGISTRY = "bug-registry.yml"
# scopes whose write lets a token post or edit an issue comment as github-actions[bot]
COMMENTING = ("issues", "pull-requests")


def _unquote(text: str) -> str:
    text = text.strip()
    return text[1:-1] if len(text) > 1 and text[0] == text[-1] and text[0] in "'\"" else text


def _bare(line: str) -> str:
    """The line without a trailing comment (a # after a space), right-stripped."""
    return re.sub(r"\s+#.*$", "", line).rstrip()


def permission_blocks(text: str) -> list:
    """Every permissions block of a workflow: [(indent, value)], where value is the scalar it is set to
    (write-all, read-all, '{}') or a dict of scope -> access. A block this reader cannot read raises ValueError,
    so the test fails instead of passing a form it does not understand."""
    if re.search(r"(^|\s)[&*][A-Za-z0-9_-]+|<<:", text):
        raise ValueError("YAML anchors, aliases or merge keys: read the permissions by hand")
    lines = text.splitlines()
    out = []
    for i, raw in enumerate(lines):
        line = _bare(raw)
        m = re.match(r"^(\s*)(?:-\s+)?[\"']?permissions[\"']?\s*:(.*)$", line)
        if not m:
            continue
        indent, rest = len(m.group(1)), m.group(2).strip()
        if rest.startswith("{"):
            if not rest.endswith("}"):
                raise ValueError(f"line {i + 1}: a flow mapping over several lines")
            body = rest[1:-1].strip()
            scopes = {}
            for pair in filter(None, (p.strip() for p in body.split(","))):
                k, sep, v = pair.partition(":")
                if not sep:
                    raise ValueError(f"line {i + 1}: cannot read {pair!r}")
                scopes[_unquote(k)] = _unquote(v)
            out.append((indent, scopes if scopes else "{}"))
        elif rest:
            out.append((indent, _unquote(rest)))
        else:
            scopes, child = {}, None
            for nxt in lines[i + 1:]:
                bare = _bare(nxt)
                if not bare.strip() or bare.lstrip().startswith("#"):
                    continue
                depth = len(bare) - len(bare.lstrip())
                if depth <= indent:
                    break
                child = depth if child is None else child
                if depth != child:
                    raise ValueError(f"line {i + 1}: a nested value under permissions")
                k, sep, v = bare.strip().partition(":")
                if not sep or not v.strip():
                    raise ValueError(f"line {i + 1}: cannot read {bare.strip()!r}")
                scopes[_unquote(k)] = _unquote(v)
            if not scopes:
                raise ValueError(f"line {i + 1}: an empty permissions block")
            out.append((indent, scopes))
    return out


def writes_issues(value) -> bool:
    if isinstance(value, str):
        return value not in ("{}", "read-all")
    return any(value.get(s) == "write" for s in COMMENTING)


class RegistryPermissionTests(unittest.TestCase):
    def test_only_the_registry_workflow_can_write_issues(self):
        files = sorted(list(WORKFLOWS.glob("*.yml")) + list(WORKFLOWS.glob("*.yaml")))
        self.assertIn(REGISTRY, [f.name for f in files])
        for wf in files:
            with self.subTest(workflow=wf.name):
                blocks = permission_blocks(wf.read_text())
                self.assertTrue(any(indent == 0 for indent, _ in blocks),
                                "no top-level permissions block: the repository's default token applies")
                writers = [value for _, value in blocks if writes_issues(value)]
                if wf.name != REGISTRY:
                    self.assertEqual(writers, [], "only the bug registry's workflow may write issues")

    def test_the_registry_grants_issues_write_to_its_one_job_only(self):
        blocks = permission_blocks((WORKFLOWS / REGISTRY).read_text())
        self.assertEqual([v for indent, v in blocks if indent == 0], ["{}"])
        self.assertEqual([v for indent, v in blocks if indent > 0], [{"issues": "write", "contents": "read"}])

    def test_the_reader_sees_every_form_that_grants_write(self):
        cases = {"permissions: write-all\n": True,
                 "permissions:\n  contents: read\n": False,
                 "permissions: {issues: write}\n": True,
                 "permissions: {}\n": False,
                 "permissions: read-all\n": False,
                 "jobs:\n  a:\n    permissions:\n      'pull-requests': \"write\" # comment\n": True,
                 "jobs:\n  a:\n    permissions:\n      issues: read\n": False}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(any(writes_issues(v) for _, v in permission_blocks(text)), want)
        for text in ("permissions:\n", "x: &p {issues: write}\npermissions: *p\n", "permissions: {issues: write,\n"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    permission_blocks(text)


if __name__ == "__main__":
    unittest.main()
