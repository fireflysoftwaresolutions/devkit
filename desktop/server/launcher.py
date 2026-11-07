#!/usr/bin/env python3
# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""devkit launcher: the entry point of devkit's Claude Desktop extension (an MCPB bundle).

It finds the devkit checkout on this Mac and replaces itself with `<devkit>/bin/dev mcp`, the MCP server that lives
in the checkout, so updating devkit (`dev update`) updates the tools without reinstalling the extension. It looks,
in this order, for a folder that holds devkit with `dev mcp`:

  1. DEVKIT_HOME: the extension's "devkit folder" setting, when it is set
  2. "devkit_home" in ~/.config/devkit/config.json ($DEVKIT_CONFIG), when present
  3. the `dev` command that `dev setup` links in ~/.local/bin (it points into the checkout)
  4. <dev_root>/fireflysoftwaresolutions/devkit, dev_root from $DEV_ROOT or that config file (default ~/Development)

When none does, it serves a small MCP server whose one tool, devkit_status, explains how to install or update devkit.

  python3 launcher.py            serve on stdio (Claude Desktop runs this)
  python3 launcher.py --check    print where devkit was found, as JSON, and exit (0 found, 1 not found)

Standard library only; runs on the macOS system Python 3.9.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
REPO = "fireflysoftwaresolutions/devkit"
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def _config() -> dict:
    path = os.environ.get("DEVKIT_CONFIG") or os.path.join(HOME, ".config", "devkit", "config.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _setting(value) -> str:
    """A user_config value from Claude Desktop: empty, or an unfilled ${…} placeholder, means not set."""
    value = (value or "").strip()
    return "" if not value or "${" in value else os.path.expanduser(value)


def candidates() -> list:
    """(source, folder) pairs in the order they are tried."""
    cfg = _config()
    out = []
    if _setting(os.environ.get("DEVKIT_HOME")):
        out.append(("DEVKIT_HOME", _setting(os.environ.get("DEVKIT_HOME"))))
    if _setting(cfg.get("devkit_home")):
        out.append(("config devkit_home", _setting(cfg.get("devkit_home"))))
    link = os.path.join(HOME, ".local", "bin", "dev")
    if os.path.lexists(link):
        out.append(("~/.local/bin/dev", os.path.dirname(os.path.dirname(os.path.realpath(link)))))
    root = os.environ.get("DEV_ROOT") or _setting(cfg.get("dev_root")) or os.path.join(HOME, "Development")
    out.append(("dev_root", os.path.join(os.path.expanduser(root), *REPO.split("/"))))
    return out


def locate() -> dict:
    """-> {"ok", "home", "source", "tried": [...], "python"}; ok is False when no usable checkout was found."""
    tried, old = [], None
    if sys.version_info < (3, 9):
        return {"ok": False, "home": None, "source": None, "tried": [], "python": sys.version.split()[0],
                "reason": "devkit needs Python 3.9 or later; Claude Desktop started " + sys.executable + " "
                          + sys.version.split()[0] + "."}
    for source, folder in candidates():
        folder = os.path.realpath(folder)
        dev = os.path.join(folder, "bin", "dev")
        is_devkit = os.path.isfile(dev) and os.path.isfile(os.path.join(folder, "lib", "devkit", "cli.py"))
        has_mcp = is_devkit and os.path.isfile(os.path.join(folder, "lib", "devkit", "mcp.py"))
        tried.append({"source": source, "folder": folder,
                      "result": "ok" if has_mcp else ("devkit without `dev mcp` (too old)" if is_devkit else "no devkit")})
        if has_mcp:
            return {"ok": True, "home": folder, "source": source, "tried": tried, "python": sys.version.split()[0]}
        if is_devkit and old is None:
            old = folder
    reason = ("devkit at " + old + " is older than this extension: it has no `dev mcp`." if old
              else "No devkit checkout was found on this Mac.")
    return {"ok": False, "home": None, "source": None, "tried": tried, "python": sys.version.split()[0],
            "reason": reason, "old": old}


def _version() -> str:
    try:
        with open(os.path.join(HERE, "..", "manifest.json")) as fh:
            return json.load(fh).get("version", "0.0.0")
    except (OSError, ValueError):
        return "0.0.0"


def help_text(found: dict) -> str:
    looked = "\n".join("  - " + t["folder"] + " (" + t["source"] + "): " + t["result"] for t in found["tried"])
    steps = (["Update it: run `dev update` in a terminal."] if found.get("old") else
             ["Install it: `gh repo clone " + REPO + " ~/Development/" + REPO + "`, then run `./install.sh` in that "
              "folder (it runs `dev setup`). The repository is private: it needs your GitHub access."])
    steps.append("If devkit is in another folder, choose it in Claude Desktop → Settings → Extensions → devkit → "
                 "devkit folder.")
    steps.append("Then turn the devkit extension off and on (or restart Claude Desktop), so it starts again.")
    return ("devkit is not available to Claude Desktop on this Mac. " + found["reason"] + "\n\n"
            + ("Looked in:\n" + looked + "\n\n" if looked else "")
            + "To fix it:\n" + "\n".join(str(n) + ". " + s for n, s in enumerate(steps, 1)))


STATUS_TOOL = {
    "name": "devkit_status",
    "title": "devkit setup",
    "description": "Why devkit's tools are not available in Claude Desktop on this Mac, and the steps to fix it. "
                   "Call it when the developer asks about devkit, their projects, tasks or status, and explain the "
                   "steps to them.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "annotations": {"title": "devkit setup", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                    "openWorldHint": False},
}


def rescue(found: dict) -> int:
    """A minimal MCP server (JSON-RPC 2.0 over stdio, one message per line) with one tool: devkit_status."""
    out = sys.stdout.buffer

    def send(msg):
        out.write(json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        out.flush()

    def reply(mid, result=None, error=None):
        send({"jsonrpc": "2.0", "id": mid, "error": error} if error else {"jsonrpc": "2.0", "id": mid, "result": result})

    sys.stderr.write("[devkit-launcher] " + found["reason"] + " Serving devkit_status only.\n")
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            reply(None, error={"code": -32700, "message": "Parse error"})
            continue
        if not isinstance(msg, dict) or "method" not in msg:
            if not (isinstance(msg, dict) and ("result" in msg or "error" in msg)):
                reply(None, error={"code": -32600, "message": "Invalid Request"})
            continue
        if "id" not in msg:
            continue  # notifications need no answer
        mid, method, params = msg["id"], msg["method"], msg.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion") if isinstance(params, dict) else None
            reply(mid, {"protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "devkit", "version": _version()},
                        "instructions": "devkit's tools are not available on this Mac yet. When the developer asks "
                                        "about devkit, their projects, tasks or status, call devkit_status and walk "
                                        "them through its steps."})
        elif method == "ping":
            reply(mid, {})
        elif method == "tools/list":
            reply(mid, {"tools": [STATUS_TOOL]})
        elif method == "tools/call":
            if not isinstance(params, dict) or params.get("name") != STATUS_TOOL["name"]:
                reply(mid, error={"code": -32602, "message": "Unknown tool: " + str(
                    params.get("name") if isinstance(params, dict) else None)})
            else:
                reply(mid, {"content": [{"type": "text", "text": help_text(found)}]})
        else:
            reply(mid, error={"code": -32601, "message": "Method not found: " + str(method)})
    return 0


def main() -> int:
    found = locate()
    if "--check" in sys.argv[1:]:
        sys.stdout.write(json.dumps(found, indent=1) + "\n")
        return 0 if found["ok"] else 1
    if not found["ok"]:
        return rescue(found)
    dev = os.path.join(found["home"], "bin", "dev")
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable, "-I", "-S", dev, "mcp"])  # as bin/dev's shebang: no planted code
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main())
