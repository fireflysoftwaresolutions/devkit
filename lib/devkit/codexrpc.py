# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Minimal JSON-RPC client for `codex app-server` (stdio).

devkit configures Codex through Codex's own API instead of editing config.toml by
hand: Codex then writes its file in its own format, validates values, and
computes hook-trust hashes itself (hooks/list -> currentHash).
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

    def trust_hooks(self, cwd, source_path):
        """Trust every hook declared in `source_path` (our own hooks.json). -> number trusted."""
        pending = {h["key"]: h["currentHash"] for h in self.hooks(cwd)
                   if str(h.get("sourcePath")) == str(source_path) and h.get("trustStatus") in ("untrusted", "modified")}
        if pending:
            self.write([("hooks.state", {k: {"trusted_hash": v} for k, v in pending.items()}, "upsert")])
        return len(pending)
