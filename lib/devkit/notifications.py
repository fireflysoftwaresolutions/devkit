# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Branded macOS alerts, independent of durable messages and their acknowledgements.

Call notify_event only after committing an event and releasing its record locks. A true result means the native
notification center accepted it (or this event was accepted earlier), never that someone saw or read it.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from xml.parsers.expat import ExpatError

from . import core, state

BUNDLE_ID = "com.fireflysoftwaresolutions.devkit.notifications"
APP = core.HOME / "Applications" / "DevKit.app"
ATTEMPTS = core.STATE / "notification-attempts"
LEASE_SECONDS = 15
_CATEGORIES = {"crew", "peer", "decision", "security", "progress"}


def _ready() -> bool:
    if sys.platform != "darwin":
        return False
    paths = [APP, APP / "Contents", APP / "Contents/MacOS", APP / "Contents/Resources",
             APP / "Contents/MacOS/DevKit", APP / "Contents/Info.plist", APP / "Contents/Resources/DevKit.icns"]
    if any(p.is_symlink() or not p.exists() for p in paths):
        return False
    try:
        info = plistlib.loads(paths[-2].read_bytes())
    except ExpatError:
        return False
    return (isinstance(info, dict) and info.get("CFBundleIdentifier") == BUNDLE_ID and info.get("CFBundleDisplayName") == "DevKit"
            and info.get("CFBundleExecutable") == "DevKit" and info.get("CFBundleIconFile") == "DevKit.icns"
            and paths[-1].read_bytes()[:4] == b"icns")


def _send(identifier: str, category: str) -> bool:
    try:
        result = subprocess.run([str(APP / "Contents/MacOS/DevKit"), "--notify"],
                                input=json.dumps({"id": identifier, "title": "DevKit", "category": category}),
                                text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def notify_event(event: dict) -> bool:
    """Attempt a metadata-only alert. False is retryable; never reads or writes message stores.

Only event_id and recipient form the dedupe identity. Bodies, summaries, references, and supplied titles never
reach the helper. A separate attempt lease bounds duplicate concurrent calls and recovers after interruption.
"""
    if sys.platform != "darwin" or not isinstance(event, dict):
        return False
    identity = [event.get("event_id"), event.get("recipient")]
    if any(not isinstance(v, str) or not v or len(v) > 4096 for v in identity):
        return False
    key = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode("ascii")).hexdigest()
    category = event.get("category")
    category = category if isinstance(category, str) and category in _CATEGORIES else "progress"
    token = uuid.uuid4().hex
    record = ATTEMPTS / (key + ".json")
    try:
        if not _ready():
            return False
        if ATTEMPTS.is_symlink():
            return False
        ATTEMPTS.mkdir(mode=0o700, parents=True, exist_ok=True)
        if record.is_symlink() or (ATTEMPTS / "locks").is_symlink():
            return False
        with state.hold(record, wait=0.1):
            previous = state.load(record) or {}
            if previous.get("accepted"):
                return True
            if previous.get("until", 0) > time.time():
                return False
            state.replace(record, {"token": token, "until": time.time() + LEASE_SECONDS, "accepted": False})
        accepted = _send(key, category)
        with state.hold(record, wait=0.1):
            current = state.load(record) or {}
            if current.get("token") == token:
                state.replace(record, {"token": token, "until": 0, "accepted": accepted})
        return accepted
    except (OSError, ValueError, TypeError):
        return False


def notify(title: str, text: str, subtitle: str = "", category: str = "progress") -> bool:
    """Compatibility for non-message notices; content is deliberately generic and never carries supplied text. The
    category picks the helper's generic text (a security notice never reads as progress)."""
    category = category if category in _CATEGORIES else "progress"
    try:
        return _ready() and _send(hashlib.sha256(uuid.uuid4().bytes).hexdigest(), category)
    except (OSError, ValueError, TypeError):
        return False


def _run(argv):
    result = subprocess.run([str(v) for v in argv], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("Cannot build DevKit notifications: " + str(argv[0]) +
                           " failed. Check Xcode Command Line Tools, then rerun dev install --only cli.")
    return result


def build_bundle(source: Path, destination: Path) -> Path:
    """Build/sign a native app from a pinned source tree; no launch, notification, or permission request.

The old app stays available until the staged bundle verifies. Repeated installs reuse an intact matching build.
"""
    source, destination = Path(source), Path(destination)
    native = source / "templates" / "macos" / "DevKit.m"
    icon = source / "plugin" / "assets" / "icon.png"
    fingerprint = hashlib.sha256(native.read_bytes() + icon.read_bytes() +
                                 core.VERSION.encode() + BUNDLE_ID.encode()).hexdigest()
    if destination.is_symlink():
        raise ValueError("DevKit.app is a symlink; remove the link before dev install --only cli.")
    marker = destination / "Contents" / "Resources" / "source.sha256"
    if marker.is_file() and marker.read_text() == fingerprint:
        try:
            _run(["/usr/bin/codesign", "--verify", "--strict", destination])
            return destination
        except RuntimeError:
            pass  # A changed installed bundle is rebuilt from the pinned source.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".devkit-notifier-", dir=destination.parent) as temp:
        stage = Path(temp) / "DevKit.app"
        contents = stage / "Contents"
        resources, binaries = contents / "Resources", contents / "MacOS"
        resources.mkdir(parents=True)
        binaries.mkdir()
        executable = binaries / "DevKit"
        _run(["/usr/bin/xcrun", "clang", "-fobjc-arc", "-O2", "-framework", "Cocoa",
              "-framework", "UserNotifications", native, "-o", executable])
        iconset = Path(temp) / "DevKit.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                suffix = "@2x" if scale == 2 else ""
                _run(["/usr/bin/sips", "-z", size * scale, size * scale, icon,
                      "--out", iconset / f"icon_{size}x{size}{suffix}.png"])
        _run(["/usr/bin/iconutil", "-c", "icns", iconset, "-o", resources / "DevKit.icns"])
        info = {"CFBundleIdentifier": BUNDLE_ID, "CFBundleName": "DevKit", "CFBundleDisplayName": "DevKit",
                "CFBundleExecutable": "DevKit", "CFBundleIconFile": "DevKit.icns",
                "CFBundlePackageType": "APPL", "CFBundleVersion": core.VERSION,
                "CFBundleShortVersionString": core.VERSION, "LSUIElement": True,
                "LSMinimumSystemVersion": "10.14", "NSHighResolutionCapable": True}
        (contents / "Info.plist").write_bytes(plistlib.dumps(info))
        (resources / "source.sha256").write_text(fingerprint)
        _run(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", BUNDLE_ID, stage])
        _run(["/usr/bin/codesign", "--verify", "--strict", stage])
        check = json.loads(_run([executable, "--check"]).stdout)
        if check != {"identifier": BUNDLE_ID, "name": "DevKit", "icon": True}:
            raise RuntimeError("DevKit notification identity check failed; rerun dev install --only cli.")
        retired = Path(temp) / "retired.app"
        if destination.exists():
            os.replace(destination, retired)
        try:
            os.replace(stage, destination)
        except BaseException:
            if retired.exists():
                os.replace(retired, destination)
            raise
    return destination
