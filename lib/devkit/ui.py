# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Terminal output: colours, the banner, tables. Plain text when not a TTY or NO_COLOR is set."""
from __future__ import annotations

import os
import re
import shutil
import sys

_TTY = sys.stdout.isatty()
COLOR = _TTY and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
TRUECOLOR = COLOR and os.environ.get("COLORTERM", "") in ("truecolor", "24bit")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _c(code):
    return (lambda s: f"\x1b[{code}m{s}\x1b[0m") if COLOR else (lambda s: str(s))


bold, dim, italic = _c("1"), _c("2"), _c("3")
red, green, yellow, blue, magenta, cyan = _c("31"), _c("32"), _c("33"), _c("34"), _c("35"), _c("36")
gray = _c("90")

OK, FAIL, WARN, INFO, ARROW = green("✓"), red("✗"), yellow("!"), cyan("•"), cyan("→")
ACTIVE, IDLE = green("●"), gray("○")

BANNER = [
    "██████╗ ███████╗██╗   ██╗██╗  ██╗██╗████████╗",
    "██╔══██╗██╔════╝██║   ██║██║ ██╔╝██║╚══██╔══╝",
    "██║  ██║█████╗  ██║   ██║█████╔╝ ██║   ██║   ",
    "██║  ██║██╔══╝  ╚██╗ ██╔╝██╔═██╗ ██║   ██║   ",
    "██████╔╝███████╗ ╚████╔╝ ██║  ██╗██║   ██║   ",
    "╚═════╝ ╚══════╝  ╚═══╝  ╚═╝  ╚═╝╚═╝   ╚═╝   ",
]
# The firefly (Firefly Software Solutions) and its colour mask:
#   a antenna · w wing · e eye · b body · g glow · s spark
FIREFLY = [
    "    ╲  ╱    ",
    " ▗▄▖ ◉◉ ▗▄▖ ",
    " ▝▀▘▐██▌▝▀▘ ",
    "    ▐▓▓▌    ",
    "     ▀▀  ✦  ",
    "   ·   ˚    ",
]
FIREFLY_MASK = [
    "    a  a    ",
    " www ee www ",
    " wwwbbbbwww ",
    "    bggb    ",
    "     gg  s  ",
    "   s   s    ",
]
TRAIL = "·   ˚  ✦   ⋆  ·  ✧"
_PAL24 = {"a": (140, 140, 165), "w": (185, 228, 255), "e": (255, 255, 255), "b": (96, 96, 124),
          "g": (205, 255, 70), "s": (255, 214, 90)}
_PAL256 = {"a": 245, "w": 153, "e": 231, "b": 240, "g": 154, "s": 221}
# firefly lime -> cyan -> violet
_STOPS = [(190, 255, 60), (0, 214, 255), (170, 90, 255)]
_LINE256 = [154, 49, 45, 39, 63, 99]
_SHADOW = set("╗╔╝╚║═")
FRAME_INNER = 68
TAGLINE = "one workspace · every repo in its place · agents that hand off"
TIPS = [
    "`dcd <query>` jumps into any project; `dwt <branch>` creates a worktree and jumps into it.",
    "`dev claude -w feat/x` starts Claude Code in its own worktree: parallel work without sibling copies.",
    "`dev ci` shows exactly what CI runs. Run it locally, then push once: one push, one CI run.",
    "`dev gc` is a dry run; it only ever removes work that is already safe on the remote.",
    "Say “handoff” to Claude or Codex before you stop: the note is waiting for the next session.",
    "`dev doctor` checks git, gh, Claude, Codex, skills, Docker and the background sync in one go.",
    "Worktrees reuse node_modules copy-on-write when the lockfile matches: no reinstall, ~0 bytes.",
    "Personal agent rules go in ~/.config/devkit/AGENTS.personal.md; `dev install` merges them.",
    "`dev docker up` starts Colima with Rosetta, so amd64 images run on Apple silicon.",
    "`dev canary` shows whether every Claude/Codex reply started with “DevKit ·”, proof they followed the rules.",
    "Branches are `<type>/<ticket>-<topic>`: feat/PROJ-142-invoice-export, fix/login-timeout, release/26.11.4.",
]


def width() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def _grad(t: float, k: float = 1.0):
    t = min(1.0, max(0.0, t))
    seg = 0 if t < 0.5 else 1
    u = t * 2 - seg
    a, b = _STOPS[seg], _STOPS[seg + 1]
    return tuple(int((a[i] + (b[i] - a[i]) * u) * k) for i in range(3))


def _fg(rgb, text, bold_=False):
    return f"\x1b[{'1;' if bold_ else ''}38;2;{rgb[0]};{rgb[1]};{rgb[2]}m{text}\x1b[0m"


def _gradient(line: str, t0: float = 0.0, t1: float = 1.0) -> str:
    if not TRUECOLOR:
        return line
    n = len(line)
    return "".join(_fg(_grad(t0 + (t1 - t0) * (i / max(1, n - 1))), ch) for i, ch in enumerate(line))


def _logo_row(i: int) -> str:
    line = BANNER[i]
    if TRUECOLOR:
        n = len(line)
        out = []
        for j, ch in enumerate(line):
            if ch == " ":
                out.append(" ")
            elif ch in _SHADOW:
                out.append(_fg(_grad(j / (n - 1), 0.45), ch))
            else:
                out.append(_fg(_grad(j / (n - 1)), ch, bold_=True))
        return "".join(out)
    if COLOR:
        return f"\x1b[1;38;5;{_LINE256[i]}m{line}\x1b[0m"
    return line


def _firefly_row(i: int) -> str:
    art, mask = FIREFLY[i], FIREFLY_MASK[i]
    if not COLOR:
        return art
    out = []
    for ch, m in zip(art, mask):
        if m == " ":
            out.append(ch)
        elif TRUECOLOR:
            out.append(_fg(_PAL24[m], ch, bold_=m in "ge"))
        else:
            out.append(f"\x1b[{'1;' if m in 'ge' else ''}38;5;{_PAL256[m]}m{ch}\x1b[0m")
    return "".join(out)


def _trail() -> str:
    if TRUECOLOR:
        n = len(TRAIL)
        return "".join(_fg((255, int(214 - 90 * j / n), int(90 + 120 * j / n)), ch) for j, ch in enumerate(TRAIL))
    return f"\x1b[38;5;221m{TRAIL}\x1b[0m" if COLOR else TRAIL


def logo_inline() -> str:
    """Compact one-line logo for narrow terminals and sub-headers."""
    s = "✦ devkit"
    if TRUECOLOR:
        return _fg(_PAL24["g"], "✦", True) + " " + bold(_gradient("devkit"))
    return bold(cyan(s)) if COLOR else s


def banner(context: str = "", framed: bool = True) -> str:
    """The devkit welcome art: firefly + 3D gradient logo in a double frame, version and copyright footer."""
    from .core import COPYRIGHT, VERSION
    footer_l = f"v{VERSION}" + (f" · {context}" if context else "")
    if width() < FRAME_INNER + 6 or not framed:
        return f"\n  {logo_inline()}  {dim(footer_l)}  {dim(COPYRIGHT)}\n"
    W = FRAME_INNER

    def pad(s, n=W):
        return s + " " * max(0, n - visible_len(s))

    edge = (lambda s: _gradient(s)) if TRUECOLOR else (gray if COLOR else (lambda s: s))
    top = edge("╔" + "═" * W + "╗")
    bot = edge("╚" + "═" * W + "╝")
    side_l, side_r = edge("║"), edge("║")
    rows = [" " * (W - len(TRAIL) - 3) + _trail() + "   "]
    for i in range(6):
        rows.append("  " + _firefly_row(i) + "   " + _logo_row(i))
    rows.append("")
    rows.append("  " + italic(dim(TAGLINE)) if COLOR else "  " + TAGLINE)
    rows.append("  " + dim("─" * (W - 4)))
    spark = _fg(_PAL24["g"], "✦", True) if TRUECOLOR else (yellow("✦") if COLOR else "*")
    rows.append(f"  {spark} {bold(footer_l)}")
    rows.append("  " + (_gradient(COPYRIGHT, 0.0, 1.0) if TRUECOLOR else dim(COPYRIGHT)))
    body = [f"  {side_l}{pad(r)}{side_r}" for r in rows]
    return "\n" + "\n".join(["  " + top] + body + ["  " + bot]) + "\n"


def greeting(name: str = "") -> str:
    import datetime as _dt
    h = _dt.datetime.now().hour
    part = "Good morning" if 5 <= h < 12 else "Good afternoon" if h < 19 else "Good evening"
    first = (name or "").split()[0] if name else ""
    return f"{part}{', ' + first if first else ''}."


def tip() -> str:
    import random
    return random.choice(TIPS)


def visible_len(s: str) -> int:
    return len(ANSI_RE.sub("", str(s)))


def trunc(s: str, n: int) -> str:
    s = str(s)
    if visible_len(s) <= n:
        return s
    plain = ANSI_RE.sub("", s)
    return plain[: max(1, n - 1)] + "…"


def table(rows, headers, max_widths=None, indent=2) -> str:
    """Aligned columns; ANSI-aware. max_widths: {col_index: n} truncates long cells."""
    max_widths = max_widths or {}
    rows = [[str(c) if c is not None else "" for c in r] for r in rows]
    rows = [[trunc(c, max_widths[i]) if i in max_widths else c for i, c in enumerate(r)] for r in rows]
    widths = [visible_len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], visible_len(c))
    pad = " " * indent

    def fmt(cells, style=None):
        parts = []
        for i, c in enumerate(cells):
            c = str(c)
            parts.append(c + " " * (widths[i] - visible_len(c)))
        line = pad + "  ".join(parts).rstrip()
        return style(line) if style else line

    out = [fmt(headers, lambda s: bold(s)), pad + dim("  ".join("─" * w for w in widths))]
    out += [fmt(r) for r in rows]
    return "\n".join(out)


def section(title: str) -> str:
    return "\n" + bold(cyan("▸ ")) + bold(title)


def kv(key: str, value, key_width=14) -> str:
    return f"  {dim(key.ljust(key_width))} {value}"


def err(msg: str):
    print(f"{FAIL} {msg}", file=sys.stderr)


def warn(msg: str):
    print(f"{WARN} {msg}", file=sys.stderr)
