# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Minimal JSON-RPC client for `codex app-server` (stdio).

devkit configures Codex through Codex's own API instead of editing config.toml by
hand: Codex then writes its file in its own format, validates values, and
computes hook-trust hashes itself (hooks/list -> currentHash). devkit trusts only
the hooks it writes, matched by their whole declaration (split_hooks).
"""
from __future__ import annotations

import json
import select
import subprocess
import time

from . import core


class CodexError(RuntimeError):
    pass


class CodexRPC:
    def __init__(self, timeout=30):
        if not core.have("codex"):
            raise CodexError("codex CLI not found")
        self.timeout, self._id = timeout, 0
        self.p = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.call("initialize", {"clientInfo": {"name": "devkit", "title": "devkit", "version": core.VERSION}})
        self._send({"method": "initialized"})

    def _send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def call(self, method, params=None, timeout=None):
        self._id += 1
        mid = self._id
        self._send({"id": mid, "method": method, "params": params or {}})
        end = time.time() + (timeout or self.timeout)
        while time.time() < end:
            r, _, _ = select.select([self.p.stdout], [], [], 0.5)
            if not r:
                if self.p.poll() is not None:
                    raise CodexError("codex app-server exited")
                continue
            line = self.p.stdout.readline()
            if not line:
                raise CodexError("codex app-server closed the stream")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == mid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise CodexError(f"{method}: {msg['error'].get('message', msg['error'])}")
                return msg["result"]
        raise CodexError(f"{method}: timed out")

    def close(self):
        try:
            self.p.terminate()
            self.p.wait(timeout=5)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ helpers

    def write(self, edits, reload=True):
        """edits: [(keyPath, value, 'upsert'|'replace')]"""
        return self.call("config/batchWrite", {
            "edits": [{"keyPath": k, "value": v, "mergeStrategy": m} for k, v, m in edits],
            "reloadUserConfig": reload})

    def models(self) -> list:
        res = self.call("model/list", {})
        return [m.get("id") or m.get("model") for m in (res.get("data") or res.get("models") or []) if isinstance(m, dict)]

    def read(self, cwd=None):
        return self.call("config/read", {"cwd": cwd} if cwd else {})

    def hooks(self, cwd):
        res = self.call("hooks/list", {"cwds": [str(cwd)]})
        out = []
        for entry in res.get("data", []):
            out += entry.get("hooks", [])
        return out

    def trust_hooks(self, cwd, source_path, own):
        """Trust devkit's own hooks in `source_path` that are untrusted or modified -> number trusted. `own` is the
        hooks object devkit writes there; a hook counts as devkit's only when it is declared exactly as in `own`.
        Anything else in the file (a hook an agent added, or devkit's command declared another way) is never trusted
        here: Codex does not run it until the developer reviews it with /hooks (security review G3)."""
        mine, _ = split_hooks(self.hooks(cwd), source_path, own)
        pending = {h["key"]: h["currentHash"] for h in mine if h.get("trustStatus") in ("untrusted", "modified")}
        if pending:
            self.write([("hooks.state", {k: {"trusted_hash": v} for k, v in pending.items()}, "upsert")])
        return len(pending)


# ------------------------------------------------------------------ which hooks are devkit's
# A hook's identity is what Codex hashes for trust (codex 0.166, hooks/list -> currentHash): the event, the group's
# matcher, and the handler's command, timeout, async flag, status message and context limit. commandWindows is left
# out: hooks/list does not report it, and Codex does not run it on macOS or Linux.

def _event(name) -> str:
    """PreToolUse (hooks.json), preToolUse (hooks/list) and pre_tool_use (hook keys) are one event."""
    return str(name or "").replace("_", "").lower()


def declared_hooks(hooks: dict) -> set:
    """The identity of each command hook in a hooks.json `hooks` object."""
    return {(_event(event), group.get("matcher"), h.get("command"), h.get("timeout"), bool(h.get("async", False)),
             h.get("statusMessage"), h.get("additionalContextLimit"))
            for event, groups in (hooks or {}).items() for group in groups or [] for h in group.get("hooks") or []
            if h.get("type") == "command"}


def hook_id(h: dict):
    """The identity of one hook as hooks/list reports it (the handler's fields flattened in); None if it runs no
    command."""
    if h.get("handlerType") != "command":
        return None
    return (_event(h.get("eventName")), h.get("matcher"), h.get("command"), h.get("timeoutSec"),
            bool(h.get("async", False)), h.get("statusMessage"), h.get("additionalContextLimit"))


def split_hooks(listed, source_path, own) -> tuple:
    """(devkit's, foreign): the hooks of `listed` (hooks/list) declared in `source_path`, split by whether each one is
    declared exactly as a hook of `own`, the hooks object devkit writes to that file."""
    ids = declared_hooks(own)
    mine, foreign = [], []
    for h in listed:
        if str(h.get("sourcePath")) == str(source_path):
            (mine if hook_id(h) in ids else foreign).append(h)
    return mine, foreign
