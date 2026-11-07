#!/usr/bin/env python3
# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Screenshots of the dashboard from neutral fixture data, for reviews, the UX critique and the docs.

    python3 tools/dashboard_shots.py <out dir> [--route NAME=HASH ...] [--width 1440 390] [--theme light dark]
                                     [--doc example|empty|welcome] [--height N]

It runs inside the test suite's hermetic home (tests/_hermetic.py), so the page is built from the tests' fixture and
never reads or writes the developer's devkit. With --doc, Now's areas come from that document (tests/test_dashboard_now.py),
the page's clock is frozen at the document's time and the time zone is UTC, so two runs give the same pictures.
Each file is <name>-<width>-<theme>.png. It needs Chrome or the Playwright Chromium, found like the browser tests find it.
"""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tests  # noqa: E402,F401  (the hermetic home, before devkit is imported)

sys.path.insert(0, str(ROOT / "lib"))
from devkit import dashboard  # noqa: E402
from tests import test_dashboard as T  # noqa: E402


def theme_first(page: str, theme: str) -> str:
    """The page with its theme chosen before anything paints (what the theme switch stores)."""
    return page.replace("<head>", '<head><script>document.documentElement.dataset.theme="%s"</script>' % theme, 1)


def shoot(page: str, out: Path, width: int, height: int, hash_: str, env: dict) -> Path:
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "page.html"
        f.write_text(page, encoding="utf-8")
        res = subprocess.run([T.CHROME, "--headless", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
                              f"--user-data-dir={t}/profile", f"--window-size={width},{height}", "--virtual-time-budget=4000",
                              f"--screenshot={out}", f.as_uri() + hash_], capture_output=True, text=True, timeout=120, env=env)
    if res.returncode or not out.exists():
        raise SystemExit(f"Chrome made no screenshot for {out.name}: {res.stderr.strip()[-400:]}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("--route", action="append", default=[], metavar="NAME=HASH", help="default: now=#/now")
    ap.add_argument("--width", type=int, nargs="+", default=[1440, 390])
    ap.add_argument("--theme", nargs="+", choices=("light", "dark"), default=["light", "dark"])
    ap.add_argument("--doc", help="Now's areas from this document: example, empty or welcome")
    ap.add_argument("--height", type=int, help="window height (default 900, 844 under 768 px)")
    a = ap.parse_args(argv)
    if not T.CHROME:
        raise SystemExit(T.NO_CHROME)
    routes = [r.split("=", 1) for r in a.route] or [["now", "#/now"]]
    env = dict(os.environ)
    with tempfile.TemporaryDirectory() as t:
        data = T.fixture_data(Path(t) / "handoff")
    if a.doc:
        import test_dashboard_now as N   # the tests folder is on the path (tests/__init__.py)
        data = N.with_doc(data, a.doc)
        env["TZ"] = "UTC"
    page = dashboard.page(data)
    if a.doc:
        page = N.frozen(page)
    a.out.mkdir(parents=True, exist_ok=True)
    for name, hash_ in routes:
        for width in a.width:
            for theme in a.theme:
                h = a.height or (900 if width >= 768 else 844)
                print(shoot(theme_first(page, theme), a.out / f"{name}-{width}-{theme}.png", width, h, hash_, env))
    return 0


if __name__ == "__main__":
    sys.exit(main())
