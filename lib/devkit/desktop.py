# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""`dev desktop`: devkit's Claude Desktop extension, an MCPB bundle that runs `dev mcp`.

pack     stage desktop/manifest.json (version from VERSION), the launcher, LICENSE, NOTICE and the icon; check the
         manifest's tools against `dev mcp`'s; validate with the pinned mcpb CLI when npx is available; write
         dist/desktop/devkit-<version>.mcpb (a zip with manifest.json at its root)
status   whether Claude Desktop has the extension installed, its version against this checkout, whether it is
         enabled, and which devkit checkout its launcher finds
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from . import core, ui
from .core import DEVKIT_HOME, tilde

MCPB = "@anthropic-ai/mcpb@2.1.2"   # pinned: validate, pack and info
SRC = DEVKIT_HOME / "desktop"
DIST = DEVKIT_HOME / "dist" / "desktop"
ICON = DEVKIT_HOME / "plugin" / "assets" / "logo.png"  # the devkit icon at 512 px (icon.png is the 128 px copy)
EXT_ID = "local.mcpb.firefly-software-solutions-inc..devkit"
FILES = ("manifest.json", "server/launcher.py", "icon.png", "LICENSE", "NOTICE")


def claude_dir() -> Path:
    return core.HOME / "Library" / "Application Support" / "Claude"


def source_manifest() -> dict:
    return json.loads((SRC / "manifest.json").read_text())


def check_tools(manifest: dict) -> list:
    """Problems between the manifest's static tool list and the tools `dev mcp` serves (empty when they match)."""
    from . import mcp
    listed = [t.get("name") for t in manifest.get("tools") or []]
    served = [t["name"] for t in mcp.TOOLS]
    out = []
    missing, extra = [n for n in served if n not in listed], [n for n in listed if n not in served]
    if missing:
        out.append("manifest.json lacks tools the server has: " + ", ".join(missing))
    if extra:
        out.append("manifest.json lists tools the server does not have: " + ", ".join(extra))
    if not out and listed != served:
        out.append("manifest.json lists the tools in another order than the server: " + ", ".join(served))
    out += [f"manifest.json: tool {t.get('name')} has no description" for t in manifest.get("tools") or []
            if not (t.get("description") or "").strip()]
    if manifest.get("tools_generated") is not False:
        out.append("manifest.json: tools_generated must be false (the list is static)")
    return out


def stage(dest: Path) -> dict:
    """Copy the bundle's files into `dest`, the version filled in. Raises ValueError on a check that fails."""
    m = source_manifest()
    m["version"] = core.VERSION
    problems = check_tools(m)
    if problems:
        raise ValueError("; ".join(problems))
    launcher = SRC / "server" / "launcher.py"
    compile(launcher.read_text(), str(launcher), "exec")  # a syntax error must not reach Claude Desktop
    (dest / "server").mkdir(parents=True, exist_ok=True)
    (dest / "manifest.json").write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
    shutil.copyfile(launcher, dest / "server" / "launcher.py")
    shutil.copyfile(ICON, dest / "icon.png")
    for name in ("LICENSE", "NOTICE"):
        shutil.copyfile(DEVKIT_HOME / name, dest / name)
    return m


def zip_dir(src: Path, out: Path):
    """A reproducible zip: sorted entries, fixed timestamps, manifest.json at the root."""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(p for p in src.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(f.relative_to(src).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, f.read_bytes())


def verify_bundle(path: Path) -> dict:
    """Open a .mcpb and check it: the files, the manifest at the root, the version and the tools. -> manifest."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        missing = [f for f in FILES if f not in names]
        if missing:
            raise ValueError(f"{path.name} lacks {', '.join(missing)}")
        m = json.loads(z.read("manifest.json").decode("utf-8"))
    if m.get("version") != core.VERSION:
        raise ValueError(f"{path.name}: manifest version {m.get('version')} is not {core.VERSION}")
    problems = check_tools(m)
    if problems:
        raise ValueError("; ".join(problems))
    return m


def _mcpb(*args, timeout=300):
    """Run the pinned mcpb CLI through npx -> (exit code, combined output)."""
    rc, out, err = core.sh(["npx", "--yes", MCPB, *args], timeout=timeout, env=dict(os.environ, NO_COLOR="1"))
    return rc, "\n".join(x for x in (out, err) if x).strip()


def pack(out_dir: Path = None, validate: bool = True, say=print) -> dict:
    """Build dist/desktop/devkit-<version>.mcpb -> a report. Raises ValueError when a check fails."""
    out_dir = Path(out_dir or DIST)
    out = out_dir / f"devkit-{core.VERSION}.mcpb"
    rep = {"bundle": str(out), "version": core.VERSION, "packed_by": "zip", "validate": None, "info": None}
    use_mcpb = validate and core.have("npx")
    if validate and not use_mcpb:
        rep["validate"] = "skipped: npx is not installed"
    tmp = Path(tempfile.mkdtemp(prefix="devkit-mcpb-"))
    try:
        st = tmp / "devkit"
        m = stage(st)
        rep["tools"] = len(m["tools"])
        say(f"{ui.OK} staged manifest {m['version']}, launcher, icon, LICENSE, NOTICE · {len(m['tools'])} tools match `dev mcp`")
        out_dir.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()
        if use_mcpb:
            rc, text = _mcpb("validate", str(st / "manifest.json"))
            rep["validate"] = text
            if rc != 0:
                raise ValueError(f"mcpb validate failed:\n{text}")
            say(f"{ui.OK} mcpb validate: {text.splitlines()[0] if text else 'ok'}")
            rc, text = _mcpb("pack", str(st), str(out))
            if rc == 0 and out.exists():
                try:
                    verify_bundle(out)
                    rep["packed_by"] = "mcpb pack"
                except (ValueError, zipfile.BadZipFile, KeyError) as e:
                    say(f"{ui.WARN} mcpb pack output failed the check ({e}); zipping it myself")
            else:
                say(f"{ui.WARN} mcpb pack did not work (exit {rc}); zipping it myself: {text[-300:]}")
        if rep["packed_by"] == "zip":
            if out.exists():
                out.unlink()
            zip_dir(st, out)
        verify_bundle(out)
        if use_mcpb:
            rc, text = _mcpb("info", str(out))
            rep["info"] = text
            if rc != 0:
                raise ValueError(f"mcpb info failed on {out.name}:\n{text}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    data = out.read_bytes()
    rep.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    return rep


def status(base: Path = None) -> dict:
    """What Claude Desktop has installed, read from its settings folder (nothing is changed)."""
    base = Path(base or claude_dir())
    exts = (core.read_json(base / "extensions-installations.json", {}) or {}).get("extensions") or {}
    ext_id = EXT_ID if EXT_ID in exts else next(
        (k for k, e in exts.items() if ((e or {}).get("manifest") or {}).get("name") == "devkit"), None)
    ext = exts.get(ext_id) or {}
    settings = core.read_json(base / "Claude Extensions Settings" / f"{ext_id}.json", {}) if ext_id else {}
    settings = settings if isinstance(settings, dict) else {}
    home_setting = ((settings.get("userConfig") or {}).get("devkit_home") or "")
    bundles = sorted(DIST.glob("devkit-*.mcpb"), key=lambda p: p.stat().st_mtime) if DIST.exists() else []
    out = {"claude_desktop": base.exists(), "installed": bool(ext_id), "id": ext_id or EXT_ID,
           "installed_version": ext.get("version"), "version": core.VERSION,
           "current": bool(ext_id) and ext.get("version") == core.VERSION,
           "enabled": settings.get("isEnabled") if ext_id else None, "installed_at": ext.get("installedAt"),
           "signature": (ext.get("signatureInfo") or {}).get("status"), "devkit_home_setting": home_setting,
           "bundle": str(bundles[-1]) if bundles else None, "launcher": None}
    launcher = base / "Claude Extensions" / (ext_id or EXT_ID) / "server" / "launcher.py"
    if ext_id and launcher.exists():
        rc, text, _ = core.sh([sys.executable, str(launcher), "--check"], timeout=15, input="",
                              env=dict(os.environ, DEVKIT_HOME=home_setting))
        try:
            out["launcher"] = json.loads(text)
        except ValueError:
            out["launcher"] = {"ok": False, "reason": f"the installed launcher gave no answer (exit {rc})"}
    return out


def _print_status(s: dict):
    print(ui.section("Claude Desktop extension · devkit"))
    if not s["claude_desktop"]:
        print(f"  {ui.WARN} Claude Desktop is not installed for this user ({tilde(claude_dir())} is missing)")
    if not s["installed"]:
        print(f"  {ui.dim('·')} not installed")
        first = f"open {tilde(s['bundle'])}" if s["bundle"] else "build it with `dev desktop pack`, then open the .mcpb"
        print(ui.dim(f"  install: {first} (double-click) or drag it onto Claude Desktop → Settings → Extensions"))
        return
    version = f"{ui.OK} {s['installed_version']}"
    if not s["current"]:
        again = tilde(s["bundle"]) if s["bundle"] else "the bundle from `dev desktop pack`"
        version = f"{ui.WARN} {s['installed_version']}  (this checkout is {s['version']}: install {again})"
    print(ui.kv("installed", version, 12))
    enabled = {True: f"{ui.OK} yes", False: f"{ui.WARN} no (Settings → Extensions → devkit)"}.get(s["enabled"],
                                                                                              "unknown")
    print(ui.kv("enabled", enabled, 12))
    if s["installed_at"]:
        print(ui.kv("since", f"{s['installed_at'][:16].replace('T', ' ')} UTC · {s['signature'] or 'unsigned'}", 12))
    print(ui.kv("folder", s["devkit_home_setting"] or "auto (where `dev setup` installed devkit)", 12))
    ln = s.get("launcher") or {}
    if ln.get("ok"):
        print(ui.kv("devkit", f"{ui.OK} {tilde(ln['home'])} (found via {ln['source']})", 12))
    elif ln:
        print(ui.kv("devkit", f"{ui.FAIL} {ln.get('reason', 'not found')}", 12))
    print(ui.kv("id", s["id"], 12))


def cmd_desktop(a) -> int:
    if a.action == "status":
        s = status()
        if a.json:
            print(json.dumps(s, indent=1, ensure_ascii=False))
        else:
            _print_status(s)
            print()
        return 0
    try:
        rep = pack(validate=not a.no_validate, say=(lambda _m: None) if a.json else print)
    except (ValueError, OSError, zipfile.BadZipFile) as e:
        ui.err(str(e))
        return 1
    if a.json:
        print(json.dumps(rep, indent=1, ensure_ascii=False))
        return 0
    if rep.get("info"):
        print(f"{ui.OK} mcpb info:\n" + "\n".join("    " + ln for ln in rep["info"].splitlines()))
    elif rep.get("validate"):
        print(ui.dim(f"  mcpb {rep['validate']}"))
    print(f"{ui.OK} {tilde(rep['bundle'])} · {rep['size'] / 1024:.0f} KB · {rep['packed_by']} · sha256 {rep['sha256'][:16]}…")
    print(ui.dim("  install: open it (double-click) or drag it onto Claude Desktop → Settings → Extensions; "
                 "`dev desktop status` checks it afterwards"))
    return 0


def register(add):
    """Wire `dev desktop` into the parser. `add(name, fn, help)` returns an argparse subparser."""
    p = add("desktop", cmd_desktop, "Claude Desktop extension: build the .mcpb bundle, check what is installed")
    p.add_argument("action", choices=["pack", "status"])
    p.add_argument("--no-validate", action="store_true", help="pack without the mcpb CLI (no npx download)")
    p.add_argument("--json", action="store_true")
