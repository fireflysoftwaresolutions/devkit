#!/usr/bin/env python3
# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Build and scan a qualified source snapshot, retaining raw diagnostics as evidence.

Run with the workflow's digest-pinned Semgrep image. Evidence must live outside
the checkout. No rules are disabled: the gate evaluates the complete JSON report,
including parse failures and fixpoint timeouts that need not affect its exit code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from devkit import pipelines, security  # noqa: E402

RULES_CONFIG = "p/default"  # the registry pack the gate scans with, kept in the evidence as the rules that ran
RULES_URL = "https://semgrep.dev/c/p/default"  # where the registry serves it (fetch_pack)
ENGINE_VERSION = "1.182.0"
ENGINE_IMAGE = "semgrep/semgrep:1.182.0@sha256:4a34048c04fef049f71665047f074cffb00cc3d89482f965a5cd151d589237af"
NATIVE_SOURCE = "templates/macos/DevKit.m"
NATIVE_WORKFLOW = ".github/workflows/ci.yml"
NATIVE_COMMAND = ["xcrun", "clang", "--analyze", "-fobjc-arc", "-fblocks", "-Werror",
                  "-Xanalyzer", "-analyzer-werror", "-Xanalyzer", "-analyzer-output=text", NATIVE_SOURCE]
NATIVE_RUN = '''native_dir=${DEVKIT_NATIVE_EVIDENCE:-}
if [ -z "$native_dir" ]; then native_dir=$(mktemp -d "${TMPDIR:-/tmp}/devkit-native.XXXXXX"); fi
echo "Native analysis evidence: $native_dir"
python3 scripts/security_corpus.py --native-output "$native_dir/native.json"'''
EXECUTABLE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".zsh",
                       ".yml", ".yaml", ".html", ".c", ".h", ".m", ".cpp", ".go", ".java", ".rb", ".php"}
REVIEWED_RULE = "javascript.lang.security.audit.detect-non-literal-regexp.detect-non-literal-regexp"
PATH_JOIN_RULE = "javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal"
CHILD_PROCESS_RULE = "javascript.lang.security.detect-child-process.detect-child-process"
FILE_MODE_RULE = "python.lang.security.audit.insecure-file-permissions.insecure-file-permissions"
SUBPROCESS_RULE = "python.django.security.injection.command.subprocess-injection.subprocess-injection"
CSRF_RULE = "python.django.security.django-no-csrf-token.django-no-csrf-token"
SHA1_RULE = "python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1"
WEBSOCKET_RULE = "javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket"
HTTP_RULE = "problem-based-packs.insecure-transport.js-node.using-http-server.using-http-server"
# the scheme of a plain WebSocket URL, quoted from test fixtures below; written in two parts so the scan of this
# file finds no WebSocket URL of its own
WS_SCHEME = "ws:" + "//"
# Security review: every literal metacharacter is escaped before alternation;
# only these two constructor spans in this unchanged function prefix qualify.
REVIEWED_HIGHLIGHT = r'''function highlight(el, q, cls) {
  const tk = tokens(q).filter(t => t.length > 1); if (!tk.length) return 0;
  const src = "(" + tk.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")";
  const re = new RegExp(src, "gi"), probe = new RegExp(src, "i");'''
REVIEWED_DIRECTORY = '''@contextmanager
def _directory(path, *, create=False):
    """Walk with no-follow directory handles; never resolve a checked ancestor again."""
    path = _absolute(path)
    pins = _directories()
    base, fd = _open_base(path, pins, create)
    active = "messaging-v2" in base.parts
    try:
        for name in path.relative_to(base).parts:
            active = active or name == "messaging-v2"
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            try:
                child = os.open(name, flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise StateBusy("unsafe state path: symbolic link or non-directory", code=2) from None
                raise
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid not in (0, os.getuid()) or (active and info.st_uid != os.getuid()):
                raise StateBusy("unsafe state directory: owner mismatch", code=2)
            if create and active:
                os.fchmod(fd, 0o700)
        yield fd
    finally:
        os.close(fd)
'''
# Security review: the walk starts from a pinned handle, the state root resolved once and owner-checked, or /;
# every part below it is opened without following links. Both state.py dispositions require this exact text.
REVIEWED_OPEN_BASE = '''_TRUSTED = {}  # devkit's state root as given -> the real folder it resolves to, resolved once per process


def _trusted_root(path):
    """(the state root, its real folder) when `path` is the state root (core.STATE) or inside it, else (None, None).
    The root and what lies above it (a home folder reached through a link, /Users/me -> /Volumes/Data/me) are the
    system's and the user's, so they are resolved once and followed; every part below the root is walked without
    following a link."""
    from . import core
    root = _absolute(core.STATE)
    if path != root and root not in path.parents:
        return None, None
    if str(root) not in _TRUSTED:
        _TRUSTED[str(root)] = Path(os.path.realpath(root))
    return root, _TRUSTED[str(root)]


def _open_base(path, pins, create):
    """(the folder the walk starts from, its handle): a pinned ancestor, else the state root as it resolves, else /."""
    base = next((p for p in (path,) + tuple(path.parents) if p in pins), None)
    if base is not None:
        return base, os.dup(pins[base])
    root, real = _trusted_root(path)
    if root is None:
        base = Path(path.anchor)
        return base, os.open(str(base), os.O_RDONLY | os.O_DIRECTORY)
    if create:
        os.makedirs(real, 0o700, exist_ok=True)
    fd = os.open(str(real), os.O_RDONLY | os.O_DIRECTORY)
    if os.fstat(fd).st_uid not in (0, os.getuid()):
        os.close(fd)
        raise StateBusy("unsafe state directory: owner mismatch", code=2)
    return root, fd
'''
REVIEWED_STATE_WRITE = '''def _write(path: Path, doc: dict, indent, durable=False, max_bytes=None):
    text = json.dumps(doc, indent=indent, ensure_ascii=not durable,
                      separators=(",", ":") if durable and indent is None else None)
    if max_bytes is not None and len(text.encode("utf-8")) > max_bytes:
        raise StateBusy("capacity: record exceeds the storage limit", code=2)
    if durable or "messaging-v2" in path.parts:
        with _directory(path.parent, create=True) as directory:
            _record_info(directory, path.name)
            os.fchmod(directory, 0o700)
'''
REVIEWED_PERMISSION_FIXTURE = '''    def test_canonical_preparation_never_chmods_a_swapped_parent_target(self):
        import os
        import stat
        destination = peers.box_path("claude", CLAUDE_A)
        namespace = peers.PEERS.parent / "messaging-v2"
        namespace.mkdir(mode=0o700)
        outside = self.root / "outside-state"
        outside.mkdir(mode=0o755)
        os.chmod(outside, 0o755)
        original = state.secure_path
        swapped = []
        def swap_after_check(path):
            original(path)
            if path == destination and not swapped:
                namespace.rename(self.root / "held-namespace")
                namespace.symlink_to(outside, target_is_directory=True)
                swapped.append(True)
        with patch.object(state, "secure_path", side_effect=swap_after_check):
            with self.assertRaises((state.StateBusy, peers.PeerError)):
                peers._ready(destination)
        self.assertTrue(swapped)
        self.assertEqual(0o755, stat.S_IMODE(outside.stat().st_mode))
        self.assertEqual([], list(outside.iterdir()))
'''
REVIEWED_DISPOSITIONS = (
    {"rule": REVIEWED_RULE, "path": "generated/dashboard.js", "context": REVIEWED_HIGHLIGHT,
     "expressions": ('new RegExp(src, "gi")', 'new RegExp(src, "i")'),
     "reason": "highlight escapes all literal regex metacharacters before constructing alternation"},
    {"rule": "python.lang.security.audit.insecure-file-permissions.insecure-file-permissions",
     "path": "lib/devkit/state.py", "context": REVIEWED_DIRECTORY, "required_contexts": (REVIEWED_OPEN_BASE,),
     "expressions": ("os.fchmod(fd, 0o700)",),
     "reason": "tightens the fstat-validated owned directory descriptor from a no-follow walk to owner-only 0700"},
    {"rule": "python.lang.security.audit.insecure-file-permissions.insecure-file-permissions",
     "path": "lib/devkit/state.py", "context": REVIEWED_STATE_WRITE,
     "required_contexts": (REVIEWED_DIRECTORY, REVIEWED_OPEN_BASE), "expressions": ("os.fchmod(directory, 0o700)",),
     "reason": "tightens the directory descriptor returned by the ownership-checked no-follow walk to 0700"},
    {"rule": "python.lang.security.audit.insecure-file-permissions.insecure-file-permissions",
     "path": "tests/test_messaging.py", "context": REVIEWED_PERMISSION_FIXTURE,
     "required_contexts": ("class PeerIntegrityTests(PeerCase):\n",
                           "from tests.test_peers import PeerCase, CLAUDE_A, CLAUDE_B, sender\n"),
     "expressions": ("os.chmod(outside, 0o755)",),
     "reason": "hermetic swapped-parent fixture sets 0755 under PeerCase's temporary root to verify it stays unchanged"},
    # Visual diagnosis. Each path, command and mode below comes from devkit itself (its state store, `npm root -g`,
    # its own `dev impeccable`, a folder mkdtemp just made) or from a test, never from a page, a synced record or
    # another user; the test fixtures serve loopback pages to tests on purpose.
    {"rule": PATH_JOIN_RULE, "path": "lib/devkit/visual/axe.cjs",
     "context": r'''path.resolve(opts.file);
''',
     "required_contexts": (r'''  if (crypto.createHash('sha256').update(bytes).digest('hex') !== SHA256) return { skipped: 'hash mismatch' };
''',),
     "expressions": (r'''opts.file''',),
     "reason": "a test passes opts.file; production passes none and takes axe-core under `npm root -g`; either file runs "
               "only when its SHA-256 matches the pin"},
    {"rule": PATH_JOIN_RULE, "path": "lib/devkit/visual/capture.cjs",
     "context": r'''path.resolve(request.store) : null,
''',
     "expressions": (r'''request.store''',),
     "reason": "the store folder: the request arrives on stdin from devkit's own caller, store.py, which builds it from "
               "its state store; no page, record or other user sets it"},
    {"rule": PATH_JOIN_RULE, "path": "lib/devkit/visual/capture.cjs",
     "context": r'''    out.source = { file: path.resolve(source.file), page };
''',
     "required_contexts": (r'''    if (!/\.pdf$/i.test(source.file)) throw bad(`${path.basename(source.file)} is not a PDF: convert it to PDF first.`);
''',),
     "expressions": (r'''source.file''',),
     "reason": "the PDF the developer asked to capture, read as the requested input; the request arrives on stdin from "
               "devkit's own caller, store.py, which builds it from its state store; no page, record or other user sets "
               "it"},
    {"rule": PATH_JOIN_RULE, "path": "lib/devkit/visual/capture.cjs",
     "context": r'''    pkg = JSON.parse(fs.readFileSync(path.join(core, 'package.json'), 'utf8'));
''',
     "expressions": (r'''core''',),
     "reason": "Playwright's package folder: the npm root devkit's caller gives, or `npm root -g`; only package.json is "
               "read"},
    {"rule": PATH_JOIN_RULE, "path": "lib/devkit/visual/capture.cjs",
     "context": r'''    browsers = JSON.parse(fs.readFileSync(path.join(core, 'browsers.json'), 'utf8')).browsers;
''',
     "expressions": (r'''core''',),
     "reason": "Playwright's package folder: the npm root devkit's caller gives, or `npm root -g`; only browsers.json is "
               "read"},
    {"rule": PATH_JOIN_RULE, "path": "lib/devkit/visual/capture.cjs",
     "context": r'''    const parent = JSON.parse(fs.readFileSync(path.join(core, '..', '..', 'package.json'), 'utf8'));
''',
     "expressions": (r'''core''',),
     "reason": "Playwright's package folder: the npm root devkit's caller gives, or `npm root -g`; only the parent "
               "package.json is read"},
    {"rule": PATH_JOIN_RULE, "path": "lib/devkit/visual/capture.cjs",
     "context": r'''path.join(folder, profile, 'DevToolsActivePort') : null;
''',
     "required_contexts": (r'''    const profile = fs.readdirSync(folder).find((n) => n.startsWith('playwright_chromiumdev_profile-'));
''',),
     "expressions": (r'''folder''', r'''profile'''),
     "reason": "the capture's private TMPDIR folder and the Playwright profile folder found in it by its fixed prefix"},
    {"rule": CHILD_PROCESS_RULE, "path": "lib/devkit/visual/capture.cjs",
     "context": r'''      child = spawn(command, args, { cwd: req.cwd, env: process.env, stdio: ['ignore', 'pipe', 'ignore'],
        detached: true });
''',
     "required_contexts": (r'''  const [command, ...rest] = req.impeccable.command;
''',),
     "expressions": (r'''command''',),
     "reason": "an argument list with no shell; the command is devkit's own `dev impeccable` (store._impeccable, a "
               "verified engine), detached in a process group of its own so the capture's bound stops what it starts"},
    {"rule": PATH_JOIN_RULE, "path": "lib/devkit/visual/capture.cjs",
     "context": r'''  const final = path.join(folder, name);
''',
     "required_contexts": (r'''  fs.writeFileSync(tmp, bytes, { mode: 0o600, flag: 'wx' });
''',),
     "expressions": (r'''folder''', r'''name'''),
     "reason": "capture.cjs writes a picture or description only as <SHA-256 hex>.png or .json.gz, computed here from "
               "the bytes, inside blobs/ or desc/ of the store devkit's caller (store.py) names under its state folder; "
               "the temporary name adds the pid and random hex and is opened with wx"},
    {"rule": PATH_JOIN_RULE, "path": "lib/devkit/visual/capture.cjs",
     "context": r'''  const tmp = path.join(folder, `.${name}.${process.pid}.${crypto.randomBytes(6).toString('hex')}.tmp`);
''',
     "required_contexts": (r'''  fs.writeFileSync(tmp, bytes, { mode: 0o600, flag: 'wx' });
''',),
     "expressions": (r'''folder''', r'''`.${name}.${process.pid}.${crypto.randomBytes(6).toString('hex')}.tmp'''),
     "reason": "capture.cjs writes a picture or description only as <SHA-256 hex>.png or .json.gz, computed here from "
               "the bytes, inside blobs/ or desc/ of the store devkit's caller (store.py) names under its state folder; "
               "the temporary name adds the pid and random hex and is opened with wx"},
    {"rule": PATH_JOIN_RULE, "path": "lib/devkit/visual/capture.cjs",
     "context": r'''  writeAtomic(path.join(store, 'blobs'), `${result.record.image.sha256}.png`, result.image);
''',
     "expressions": (r'''store''',),
     "reason": "capture.cjs writes a picture or description only as <SHA-256 hex>.png or .json.gz, computed here from "
               "the bytes, inside blobs/ or desc/ of the store devkit's caller (store.py) names under its state folder; "
               "the temporary name adds the pid and random hex and is opened with wx"},
    {"rule": PATH_JOIN_RULE, "path": "lib/devkit/visual/capture.cjs",
     "context": r'''  if (result.crop) writeAtomic(path.join(store, 'blobs'), `${result.record.element.sha256}.png`, result.crop);
''',
     "expressions": (r'''store''',),
     "reason": "capture.cjs writes a picture or description only as <SHA-256 hex>.png or .json.gz, computed here from "
               "the bytes, inside blobs/ or desc/ of the store devkit's caller (store.py) names under its state folder; "
               "the temporary name adds the pid and random hex and is opened with wx"},
    {"rule": PATH_JOIN_RULE, "path": "lib/devkit/visual/capture.cjs",
     "context": r'''  writeAtomic(path.join(store, 'desc'), `${descSha}.json.gz`, descBytes);
''',
     "expressions": (r'''store''',),
     "reason": "capture.cjs writes a picture or description only as <SHA-256 hex>.png or .json.gz, computed here from "
               "the bytes, inside blobs/ or desc/ of the store devkit's caller (store.py) names under its state folder; "
               "the temporary name adds the pid and random hex and is opened with wx"},
    {"rule": FILE_MODE_RULE, "path": "lib/devkit/visual/capture_checks.py",
     "context": r'''    folder = tempfile.mkdtemp(prefix="devkit-visual-check-")
    try:
        os.chmod(folder, 0o700)
''',
     "expressions": (r'''os.chmod(folder, 0o700)''',),
     "reason": "owner-only 0700 on the folder mkdtemp just created for this user"},
    {"rule": FILE_MODE_RULE, "path": "lib/devkit/visual/compare.py",
     "context": r'''def _private_dir(path: Path):
    if not path.is_dir():
        _private_dir(path.parent)
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
''',
     "expressions": (r'''os.chmod(path, 0o700)''',),
     "reason": "owner-only 0700 on a folder this function just created; it adds no permission"},
    {"rule": REVIEWED_RULE, "path": "lib/devkit/visual/capture.cjs",
     "context": r'''  const named = new RegExp(`(?:^|[\\s=])${folder.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?=/|\\s|$)`);
''',
     "expressions": (r'''new RegExp(`(?:^|[\\s=])${folder.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?=/|\\s|$)`)''',),
     "reason": "the capture's own temporary folder, every regex metacharacter escaped, between fixed anchors: the "
               "pattern has no repetition an input could make backtrack"},
    {"rule": REVIEWED_RULE, "path": "lib/devkit/visual/crawl.cjs",
     "context": r'''      out += ch.replace(/[\\^$.|+()[\]{}]/g, '\\$&');
    }
  }
  return new RegExp(`^${out}$`);
''',
     "expressions": (r'''new RegExp(`^${out}$`)''',),
     "reason": "a glob of the developer's crawl exclusions: *, ** and ? become fixed classes and every other regex "
               "metacharacter is escaped"},
    {"rule": FILE_MODE_RULE, "path": "lib/devkit/visual/report.py",
     "context": r'''def _private_dir(path: Path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
''',
     "expressions": (r'''os.chmod(path, 0o700)''',),
     "reason": "owner-only 0700 on a reports folder in devkit's store, created 0700 or found there; it adds no "
               "permission"},
    {"rule": FILE_MODE_RULE, "path": "lib/devkit/visual/store.py",
     "context": r'''    folder = tempfile.mkdtemp(prefix="devkit-visual-")
    try:
        os.chmod(folder, 0o700)
''',
     "expressions": (r'''os.chmod(folder, 0o700)''',),
     "reason": "owner-only 0700 on the folder mkdtemp just created for this user"},
    {"rule": SUBPROCESS_RULE, "path": "lib/devkit/visual/store.py",
     "context": r'''        run = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
''',
     "required_contexts": (r'''        proc = subprocess.Popen([node, str(CAPTURE), "--json"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env, cwd=str(cwd), start_new_session=True)
''',),
     "expressions": (r'''subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)''',),
     "reason": "builds a result object and runs nothing; the run is an argument list with no shell: node and devkit's "
               "own capture.cjs"},
    {"rule": FILE_MODE_RULE, "path": "lib/devkit/visual/store.py",
     "context": r'''    folder = Path(tempfile.mkdtemp(prefix="devkit-visual-"))
    try:
        os.chmod(folder, 0o700)
''',
     "expressions": (r'''os.chmod(folder, 0o700)''',),
     "reason": "owner-only 0700 on the folder mkdtemp just created for this user"},
    {"rule": CSRF_RULE, "path": "tests/fixtures/visual/app/account.html",
     "context": r'''<form method="post" action="/account">
    <label>Display name <input name="display_name" value="Northwind clerk"></label>
    <button>Save</button>
  </form>
''',
     "expressions": (r'''<form method="post" action="/account">
    <label>Display name <input name="display_name" value="Northwind clerk"></label>
    <button>Save</button>
  </form>''',),
     "reason": "a test fixture page of the loopback test app, with no session to protect; the crawl test checks its POST "
               "never leaves the browser"},
    {"rule": CSRF_RULE, "path": "tests/fixtures/visual/capture/save.html",
     "context": r'''<form method="post" action="/save"><input type="hidden" name="note" value="October"><button id="save">Save</button></form>
''',
     "expressions": (r'''<form method="post" action="/save"><input type="hidden" name="note" value="October"><button id="save">Save</button></form>''',),
     "reason": "a test fixture page served on loopback to the capture's redirect tests, with no session to protect; "
               "its POST answers 303 so the tests can check every hop stays on this Mac"},
    {"rule": SHA1_RULE, "path": "tests/fixtures/visual/app/appserver.py",
     "context": r'''        def _websocket(self):
            key = self.headers.get("Sec-WebSocket-Key", "")
            accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
''',
     "required_contexts": (r'''WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
''',),
     "expressions": (r'''hashlib.sha1((key + WS_GUID).encode())''',),
     "reason": "the test server's WebSocket handshake: RFC 6455 defines Sec-WebSocket-Accept as SHA-1 of the key and "
               "this GUID; it protects nothing"},
    {"rule": WEBSOCKET_RULE, "path": "tests/fixtures/visual/app/live.html",
     "context": r'''    const socket = new WebSocket(`''' + WS_SCHEME + r'''${location.host}/socket`);
''',
     "expressions": (WS_SCHEME,),
     "reason": "a test fixture page served over http on 127.0.0.1 opens a socket to its own host"},
    {"rule": WEBSOCKET_RULE, "path": "tests/fixtures/visual/harden/index.html",
     "context": r'''try { new WebSocket(${JSON.stringify(`''' + WS_SCHEME + r'''${location.host}/ws-blob-worker`)}); } catch (_) {}`;
''',
     "expressions": (WS_SCHEME,),
     "reason": "the hardening fixture tries a socket from a blob: worker so the crawl test can see the crawler's policy "
               "refuse it"},
    {"rule": WEBSOCKET_RULE, "path": "tests/fixtures/visual/harden/worker.js",
     "context": r'''try {
  new WebSocket(`''' + WS_SCHEME + r'''${location.host}/ws-worker`);
} catch (_) {
  // a policy that refuses the socket may throw here; the server's log is what the test reads
}
''',
     "expressions": (WS_SCHEME,),
     "reason": "the hardening fixture's worker tries a socket so the crawl test can see the crawler's policy refuse it"},
    {"rule": PATH_JOIN_RULE, "path": "tests/fixtures/visual/capture/detector.cjs",
     "context": r'''    const full = path.join(dir, entry.name);
''',
     "required_contexts": (r'''function find(dir, name) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
''',),
     "expressions": (r'''dir''', r'''entry.name'''),
     "reason": "a test stand-in walks the folder it is given; each name comes from readdirSync of that folder"},
    {"rule": HTTP_RULE, "path": "tests/fixtures/visual/capture/detector.cjs",
     "context": r'''    http.get(`http://127.0.0.1:${port}/json/list`, (res) => {
''',
     "expressions": (r'''http''',),
     "reason": "a test stand-in reads the DevTools list of the capture's own port on 127.0.0.1"},
    {"rule": PATH_JOIN_RULE, "path": "tests/fixtures/visual/capture/harness.cjs",
     "context": r'''      const full = path.join(dir, entry.name);
''',
     "required_contexts": (r'''function filesUnder(folder) {
  const out = [];
  const walk = (dir) => {
    let entries = [];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch (_) {
      return;
    }
    for (const entry of entries) {
''',),
     "expressions": (r'''dir''', r'''entry.name'''),
     "reason": "a test harness walks the folder it is given; each name comes from readdirSync of that folder"},
    {"rule": HTTP_RULE, "path": "tests/fixtures/visual/capture/harness.cjs",
     "context": r'''    http.get(url, (res) => {
''',
     "required_contexts": (r'''function getJson(url) {
  return new Promise((resolve, reject) => {
''', r'''            const version = await getJson(`http://127.0.0.1:${port}/json/version`);
'''),
     "expressions": (r'''http''',),
     "reason": "a test harness reads the DevTools version of a port on 127.0.0.1"},
    {"rule": FILE_MODE_RULE, "path": "tests/test_visual_axe.py",
     "context": r'''    tmp = tempfile.mkdtemp(prefix="visual-axe-")
    try:
        os.chmod(tmp, 0o700)
''',
     "expressions": (r'''os.chmod(tmp, 0o700)''',),
     "reason": "owner-only 0700 on the folder mkdtemp just created for the test"},
    {"rule": SUBPROCESS_RULE, "path": "tests/test_visual_capture.py",
     "context": r'''    def cli(self, request, path=None, timeout=120):
        return subprocess.run([NODE, str(MODULE), "--json"], input=json.dumps(request), capture_output=True,
                              text=True, timeout=timeout, env=self.env(path), cwd=str(self.work))
''',
     "required_contexts": (r'''MODULE = ROOT / "lib" / "devkit" / "visual" / "capture.cjs"
''', r'''NODE = shutil.which("node")
'''),
     "expressions": (r'''subprocess.run([NODE, str(MODULE), "--json"], input=json.dumps(request), capture_output=True,
                              text=True, timeout=timeout, env=self.env(path), cwd=str(self.work))''',),
     "reason": "an argument list with no shell: node and devkit's capture.cjs; the request goes on stdin"},
    {"rule": FILE_MODE_RULE, "path": "tests/test_visual_describe.py",
     "context": r'''        folder = tempfile.mkdtemp(prefix="devkit-describe-")
        self.addCleanup(shutil.rmtree, folder, True)
        os.chmod(folder, 0o700)
''',
     "expressions": (r'''os.chmod(folder, 0o700)''',),
     "reason": "owner-only 0700 on the folder mkdtemp just created for the test"},
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def native_contract(source: Path) -> dict:
    """Declare complementary coverage only when the required macOS gate exists."""
    workflow = source / NATIVE_WORKFLOW
    tree = pipelines.load_yaml(workflow.read_text(encoding="utf-8"))
    job = tree.get("jobs", {}).get("test", {})
    steps = [step for step in job.get("steps", []) if step.get("name") == "Native static analysis"]
    if (not str(job.get("runs-on", "")).startswith("macos-") or job.get("continue-on-error")
            or job.get("if", "") not in ("", "github.event.pull_request.draft == false")
            or len(steps) != 1 or steps[0].get("run", "").strip() != NATIVE_RUN
            or steps[0].get("if") or steps[0].get("continue-on-error")):
        raise ValueError("native analysis requires its exact mandatory macOS workflow gate")
    return {"status": "declaration; requires separate successful macOS CI receipt",
            "source": NATIVE_SOURCE, "source_sha256": sha256(source / NATIVE_SOURCE),
            "workflow": NATIVE_WORKFLOW, "workflow_sha256": sha256(workflow),
            "job": "test", "step": "Native static analysis", "artifact": "native-analysis-<python-version>",
            "command": NATIVE_COMMAND,
            "command_sha256": hashlib.sha256(json.dumps(NATIVE_COMMAND, separators=(",", ":")).encode()).hexdigest()}


def native_receipt_matches(receipt: dict, contract: dict) -> bool:
    return (receipt.get("passed") is True and receipt.get("returncode") == 0
            and bool(receipt.get("tool_version")) and all(receipt.get(key) == contract[key] for key in (
                "source", "source_sha256", "workflow", "workflow_sha256", "command", "command_sha256")))


def native_analysis(source: Path, output: Path) -> int:
    source, output = source.resolve(), output.resolve()
    if output == source or source in output.parents:
        raise ValueError("native analysis evidence must be outside the source checkout")
    contract = native_contract(source)
    version = subprocess.run(["xcrun", "clang", "--version"], cwd=source, check=True,
                             capture_output=True, text=True, timeout=30).stdout.strip()
    result = subprocess.run(NATIVE_COMMAND, cwd=source, capture_output=True, text=True, timeout=120)
    receipt = dict(contract, status="executed native analysis", tool_version=version,
                   returncode=result.returncode, passed=result.returncode == 0,
                   stdout=result.stdout, stderr=result.stderr)
    receipt["passed"] = native_receipt_matches(receipt, native_contract(source))
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, receipt)
    print(json.dumps({"native_analysis_passed": receipt["passed"], "receipt": str(output)}))
    return 0 if receipt["passed"] else 1


def projects() -> dict:
    """Exercise every shipped pipeline template through the production generator."""
    py = {"pyproject.toml": '[project]\nname="fixture"\nversion="0.1.0"\nrequires-python=">=3.11"\n',
          "tests/test_example.py": "def test_example():\n    assert sum([1, 2]) == 3\n"}
    node = {"package.json": json.dumps({"name": "fixture", "scripts": {
        "test": "vitest run", "build": "tsc", "lint": "eslint .", "typecheck": "tsc --noEmit"}}),
        "pnpm-lock.yaml": "lockfileVersion: 9\n", ".nvmrc": "22\n"}
    stacks = {
        "python-uv": dict(py, **{"uv.lock": ""}),
        "python-pip": dict(py, **{"requirements.txt": "pytest==8.3.5\n"}),
        "python-poetry": dict(py, **{"poetry.lock": ""}),
        "node": node,
        "java-maven": {"pom.xml": "<project><properties><java.version>21</java.version></properties>"
                       "<artifactId>testcontainers</artifactId></project>", "mvnw": "#!/bin/sh\n",
                       "src/test/java/ExampleTest.java": ""},
        "java-gradle": {"build.gradle.kts": "plugins { checkstyle }\n", "gradlew": "#!/bin/sh\n",
                        "src/test/java/ExampleTest.java": ""},
        "go": {"go.mod": "module example.test/fixture\n\ngo 1.25\n", "go.sum": "",
               "main.go": "package main\nfunc main() {}\n", "main_test.go": "package main\n"},
        "php": {"composer.json": json.dumps({"require-dev": {"phpunit/phpunit": "11", "phpstan/phpstan": "2"}}),
                "composer.lock": "{}", "tests/ExampleTest.php": ""},
    }
    stacks["docker"] = dict(node, **{"Dockerfile": "FROM node:24\n"})
    stacks["mixed"] = dict(stacks["python-uv"], **stacks["docker"], **{
        "compose.test.yml": "services:\n  test:\n    build: .\n    command: echo fixture\n"})
    return stacks


def render_pipelines(templates: Path, output: Path) -> dict:
    """Map template reads to the generated file set for each stack/provider pair."""
    output.mkdir(parents=True, exist_ok=True)
    inventory = {str(p.relative_to(templates)) for p in templates.rglob("*")
                 if p.suffix in (".yml", ".yaml")}
    seen, replacements = set(), {}
    previous_templates, previous_tpl = pipelines.TEMPLATES, pipelines._tpl

    def tracked(name):
        seen.add(name)
        return (templates / name).read_text(encoding="utf-8")

    pipelines.TEMPLATES, pipelines._tpl = templates, tracked
    try:
        with tempfile.TemporaryDirectory(prefix="inputs-", dir=output) as scratch:
            for stack, files in projects().items():
                project = Path(scratch) / stack
                for name, content in files.items():
                    path = project / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                for provider in pipelines.PROVIDERS:
                    seen.clear()
                    generated, _ = pipelines.generate(project, provider,
                        policy={"tag_format": "vYY.MM.PATCH"}, branch="main")
                    artifacts = []
                    for name, content in generated.items():
                        if "@{" in content:
                            raise ValueError(f"unresolved template token in {stack}/{provider}/{name}")
                        pipelines.load_yaml(content)
                        relative = f"generated/pipelines/{stack}/{provider}/{name}"
                        path = output / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(content, encoding="utf-8")
                        artifacts.append(relative)
                    for name in seen:
                        replacements.setdefault(name, []).extend(artifacts)
        missing = inventory - replacements.keys()
        if missing:
            raise ValueError("uncovered pipeline templates: " + ", ".join(sorted(missing)))
        if replacements.keys() - inventory:
            raise ValueError("generator read a template outside the YAML inventory")
        return {"templates/ci/" + name: {"artifacts": sorted(set(paths)),
                "reason": "production pipelines.generate dependency; stack/provider output set"}
                for name, paths in sorted(replacements.items())}
    finally:
        pipelines.TEMPLATES, pipelines._tpl = previous_templates, previous_tpl


def source_files(source: Path) -> list:
    result = subprocess.run(["git", "-c", f"safe.directory={source}", "ls-files", "--cached", "--others",
                             "--exclude-standard", "-z"],
                            cwd=source, check=True, capture_output=True)
    names = sorted(set(result.stdout.decode().split("\0")) - {""})
    files = []
    for name in names:
        path = source / name
        if path.is_symlink():
            raise ValueError(f"source symlink needs an explicit scan policy: {name}")
        if path.is_file():
            files.append(name)
    return files


def build(source: Path, output: Path) -> dict:
    source, output = source.resolve(), output.resolve()
    if output == source or source in output.parents:
        raise ValueError("security evidence must be outside the source checkout")
    output.mkdir(parents=True, exist_ok=True)
    qualified, raw = output / "qualified", output / "raw"
    qualified.mkdir()
    raw.mkdir()
    files = source_files(source)
    hashes = {name: sha256(source / name) for name in files}
    native = native_contract(source)
    if NATIVE_SOURCE not in hashes:
        raise ValueError("native source is missing from the git source inventory")
    replacements = render_pipelines(source / "templates/ci", qualified)
    web = source / "lib/devkit/web"
    sections = sorted((web / "sections").glob("*.js"))
    parts = [web / "app.js", *sections, web / "boot.js"]
    # Keep the production dashboard.JS concatenation byte-for-byte; the test
    # compares the result with that production constant to catch order drift.
    script = parts[0].read_text(encoding="utf-8") + "\n".join(
        p.read_text(encoding="utf-8") for p in sections) + parts[-1].read_text(encoding="utf-8")
    (qualified / "generated/dashboard.js").write_text(script, encoding="utf-8")
    for part in parts:
        replacements[str(part.relative_to(source))] = {"artifacts": ["generated/dashboard.js"],
                                                       "reason": "production dashboard.JS ordered concatenation"}
    if replacements.keys() - hashes.keys():
        raise ValueError("replacement source missing from git source inventory")
    if any(name.startswith("generated/") for name in files):
        raise ValueError("source collides with the generated artifact namespace")
    for name in files:
        target = (raw if name in replacements else qualified) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    # An explicit empty file disables Semgrep's built-in test/ and tests/ skips.
    # The qualified tree retains the repository's own documented ignore policy.
    (raw / ".semgrepignore").write_text("", encoding="utf-8")
    if not (qualified / ".semgrepignore").is_file():
        raise ValueError("qualified scan requires the repository's explicit .semgrepignore")
    artifacts = {str(p.relative_to(qualified)): sha256(p)
                 for p in sorted((qualified / "generated").rglob("*")) if p.is_file()}
    manifest = {"schema": 1, "engine": {"version": ENGINE_VERSION, "image": ENGINE_IMAGE},
                "rules": {"config": RULES_CONFIG, "identity": "not retained yet: a scan keeps them in rules.yml"},
                "sources": hashes, "artifacts": artifacts, "replacements": replacements,
                "dashboard_order": [str(p.relative_to(source)) for p in parts],
                "qualified_sources": [n for n in files if n not in replacements],
                "complementary_coverage": {NATIVE_SOURCE: native},
                "required_sources": [n for n in files if n not in replacements and n != NATIVE_SOURCE and (
                    Path(n).suffix in EXECUTABLE_SUFFIXES or (source / n).read_bytes().startswith(b"#!"))],
                "passed": False}
    write_json(output / "manifest.json", manifest)
    return manifest


def scan_errors(report: dict, replacements=None) -> list:
    """Only raw syntax errors for an explicitly mapped input are expected."""
    replacements = replacements or {}
    errors = []
    for error in report.get("errors", []):
        path = error.get("path", "").removeprefix("./")
        kind = error.get("type")
        syntax = kind in ("Syntax error", "Other syntax error", "Partial parsing")
        if isinstance(kind, list) and len(kind) == 2 and kind[0] == "PartialParsing":
            syntax = bool(kind[1]) and all(location.get("path", "").removeprefix("./") in replacements
                                          for location in kind[1])
        if syntax and path in replacements:
            continue
        errors.append(error)
    errors.extend(report.get("time", {}).get("fixpoint_timeouts", []))
    errors.extend(report.get("time", {}).get("timeouts", []))
    errors.extend(report.get("skipped_rules", []))
    return errors


def finding_disposition(finding: dict, qualified: Path):
    """A reviewed rule/path alone grants nothing: context and exact span must match (security.disposition, the rule
    `dev sec scan` applies to the same dispositions)."""
    return security.disposition(finding, qualified, REVIEWED_DISPOSITIONS)


def reviewed_finding(finding: dict, qualified: Path) -> bool:
    return finding_disposition(finding, qualified) is not None


def evaluate(report: dict, returncode: int, qualified: Path) -> dict:
    errors = scan_errors(report)
    reviewed, findings, dispositions = [], [], []
    for finding in report.get("results", []):
        disposition = finding_disposition(finding, qualified)
        if disposition:
            reviewed.append(finding)
            dispositions.append(disposition)
        else:
            findings.append(finding)
    complete = all(key in report for key in ("results", "errors", "paths")) and bool(
        report.get("paths", {}).get("scanned")) and report.get("version") == ENGINE_VERSION
    exit_consistent = returncode == 0 or (returncode == 1 and bool(reviewed or findings))
    return {"passed": complete and exit_consistent and not errors and not findings,
            "complete": complete and exit_consistent and not errors,
            "exit_consistent": exit_consistent, "errors": errors, "findings": findings,
            "reviewed_findings": reviewed, "dispositions": dispositions}


def evaluate_raw(report: dict, returncode: int, replacements: dict) -> dict:
    errors = scan_errors(report, replacements)
    expected = [error for error in report.get("errors", []) if error not in errors]
    # --error maps findings to 1. A syntax failure (2/3) needs a matching
    # diagnostic for a mapped input; an unexplained nonzero exit never passes.
    exit_consistent = (returncode == 0 or (returncode == 1 and bool(report.get("results")))
                       or (returncode in (2, 3) and any(error.get("code") == returncode for error in expected)))
    complete = all(key in report for key in ("results", "errors", "paths")) and bool(
        report.get("paths", {}).get("scanned")) and report.get("version") == ENGINE_VERSION
    return {"passed": complete and exit_consistent and not errors, "exit_consistent": exit_consistent,
            "unexpected_errors": errors,
            "expected_errors": [dict(error, replacements=replacements[error["path"].removeprefix("./")])
                                for error in expected]}


def coverage_gaps(manifest: dict, report: dict) -> list:
    scanned = {p.removeprefix("./") for p in report.get("paths", {}).get("scanned", [])}
    return sorted((manifest["artifacts"].keys() | set(manifest["required_sources"])) - scanned)


def fetch_pack() -> bytes:
    """The p/default rule pack as the registry serves it now, as YAML (RULES_URL)."""
    # a literal https URL: nothing a caller passes reaches it
    with urllib.request.urlopen("https://semgrep.dev/c/p/default", timeout=120) as response:  # nosec B310 nosemgrep: dynamic-urllib-use-detected
        return response.read()


def retain_rules(output: Path, replay: Path = None) -> dict:
    """The rules both scans run, kept in the evidence as rules.yml: the pack fetched now, or a rules file kept by an
    earlier run (`replay`, --rules), so a replay scans with the same definitions. A pack with no rule fails closed.
    -> the manifest's rules record: where they came from, their sha256 and rule ids."""
    data = replay.read_bytes() if replay else fetch_pack()
    ids = sorted({m.decode() for m in re.findall(rb"(?m)^- id: *(\S+)", data)})
    if not ids:
        raise ValueError("the rule pack holds no rules")
    target = output / "rules.yml"
    target.write_bytes(data)
    return {"config": RULES_CONFIG, "source": f"replay of {replay}" if replay else RULES_URL, "file": target.name,
            "sha256": sha256(target), "rule_ids": ids}


def scan(tree: Path, output: Path, name: str) -> tuple:
    # `dev sec scan` and this gate share one Semgrep command (security.semgrep_command): serial, with no per-rule
    # wall-clock timeout, and package rules left out only where no file imports their packages. Both scans run the
    # rules retain_rules kept in the evidence.
    command = security.semgrep_command(tree, config=str(output / "rules.yml")) + [
        "--error", "--max-target-bytes", "0", "--no-git-ignore", "."]
    with (output / f"{name}.json").open("w") as stdout, (output / f"{name}.log").open("w") as stderr:
        result = subprocess.run(command, cwd=tree, stdout=stdout, stderr=stderr, timeout=2700)
    report = json.loads((output / f"{name}.json").read_text(encoding="utf-8"))
    return report, {"command": command, "cwd": str(tree), "returncode": result.returncode,
                    "left_out_rules": [{"rule": rule, "packages_not_imported": list(packages)}
                                       for rule, packages in security.semgrep_left_out(command)],
                    "report_sha256": sha256(output / f"{name}.json")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--native-output", type=Path, help="run the separate required macOS native analyzer")
    parser.add_argument("--render-only", action="store_true", help="generate evidence inputs without claiming a scan")
    parser.add_argument("--rules", type=Path, help="scan with this rules file, kept by an earlier run, instead of the "
                                                   "pack the registry serves now")
    args = parser.parse_args(argv)
    try:
        if args.native_output:
            if args.render_only:
                raise ValueError("--render-only does not apply to native analysis")
            return native_analysis(ROOT, args.native_output)
        output = args.output.resolve()
        manifest = build(ROOT, output)
        if args.render_only:
            print(f"Rendered {len(manifest['artifacts'])} artifacts; manifest: {output / 'manifest.json'}")
            return 0
        version = subprocess.run(["semgrep", "--version"], check=True, capture_output=True, text=True).stdout.strip()
        manifest["engine"]["observed_version"] = version
        manifest["engine"]["version_command"] = ["semgrep", "--version"]
        write_json(output / "manifest.json", manifest)
        if version != ENGINE_VERSION:
            raise ValueError(f"expected Semgrep {ENGINE_VERSION}; got {version}")
        manifest["rules"] = retain_rules(output, args.rules)
        write_json(output / "manifest.json", manifest)
        raw, raw_run = scan(output / "raw", output, "raw")
        raw_verdict = evaluate_raw(raw, raw_run["returncode"], manifest["replacements"])
        manifest["raw_scan"] = dict(raw_run, **raw_verdict)
        write_json(output / "manifest.json", manifest)
        report, run = scan(output / "qualified", output, "qualified")
        verdict = evaluate(report, run["returncode"], output / "qualified")
        missing = coverage_gaps(manifest, report)
        changed = [name for name, digest in manifest["sources"].items()
                   if not (ROOT / name).is_file() or sha256(ROOT / name) != digest]
        added = sorted(set(source_files(ROOT)) - manifest["sources"].keys())
        manifest["qualified_scan"] = dict(run, **verdict, unscanned_sources_or_artifacts=missing,
                                         changed_sources=changed, added_sources=added)
        manifest["passed"] = bool(verdict["passed"] and raw_verdict["passed"] and not missing
                                  and not changed and not added)
        write_json(output / "manifest.json", manifest)
        print(json.dumps({"passed": manifest["passed"], "manifest": str(output / "manifest.json"),
                          "findings": len(verdict["findings"]), "reviewed": len(verdict["reviewed_findings"]),
                          "errors": len(verdict["errors"]) + len(raw_verdict["unexpected_errors"]),
                          "unscanned_sources_or_artifacts": missing}))
        return 0 if manifest["passed"] else 1
    except (OSError, ValueError, subprocess.SubprocessError) as exc:  # urllib's errors are OSErrors
        print(f"Security corpus incomplete: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
